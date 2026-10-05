/************************************************************************
 * File : tcp_event_streamer.cpp                                        *
 *                                                                      *
 * Streams CD events from a Prophesee Onboard camera over a raw TCP     *
 * socket, for consumption by network_reader.py's NetworkEventsIterator *
 * (event-camera-viewer). No display/GUI — safe to run headless over    *
 * SSH.                                                                 *
 *                                                                      *
 * Wire protocol (all integers little-endian — both the Onboard's ARM   *
 * Linux and the viewer's x86 Linux host are little-endian, so no byte- *
 * swapping is done):                                                   *
 *                                                                      *
 *   Handshake, sent once as soon as a client connects:                 *
 *       char[4]  magic     "PECD"                                      *
 *       uint32_t version   protocol version (currently 1)              *
 *       uint16_t width                                                 *
 *       uint16_t height                                                *
 *                                                                      *
 *   Then, repeated for the life of the connection:                     *
 *       uint32_t count               number of events in this batch    *
 *       count * WireEvent            16 bytes each, see below          *
 *                                                                      *
 * Based on Prophesee driver samples, Copyright (c) 2018 Prophesee      *
 ************************************************************************/

#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <iostream>
#include <mutex>
#include <string>
#include <vector>

#include <boost/program_options.hpp>
#include <prophesee_driver.h>

namespace po = boost::program_options;

namespace {

std::atomic<bool> g_signal_caught(false);

void sig_handler(int) {
    g_signal_caught = true;
}

#pragma pack(push, 1)
struct WireHeader {
    char magic[4];
    uint32_t version;
    uint16_t width;
    uint16_t height;
};

// Layout matches EVENT_CD_DTYPE in hdf5_reader.py (x@0, y@2, p@4, t@8,
// itemsize 16) byte-for-byte, so the Python side can np.frombuffer() this
// directly with no repacking. _reserved is the implicit padding between p
// and t that gives PeriodicFrameGenerationAlgorithm's expected dtype its
// 8-byte alignment for t — always written as 0.
struct WireEvent {
    uint16_t x;
    uint16_t y;
    int16_t p;
    int16_t _reserved;
    int64_t t;
};
#pragma pack(pop)

static_assert(sizeof(WireHeader) == 12, "WireHeader must be 12 bytes on the wire");
static_assert(sizeof(WireEvent) == 16, "WireEvent must be 16 bytes on the wire");

constexpr uint32_t kProtocolVersion = 1;
// Cap on how much backlog gets coalesced into a single network message (see
// the send loop in main): bounds worst-case message size (~3.2MB) without
// meaningfully limiting how much a burst can be coalesced.
constexpr size_t kMaxEventsPerMessage = 200000;

enum class PopResult { kGot, kTimeout, kStopped };

/// Thread-safe hand-off between the camera's CD callback (producer, called
/// on the driver's own thread) and the socket-writer thread (consumer).
/// Batches (rather than a single flat buffer) keep each callback's copy
/// O(batch size), and let the writer thread block only on network I/O,
/// never on the camera thread.
class EventQueue {
public:
    // max_queued_events is an OOM circuit breaker, NOT a lag/latency control:
    // for lossless capture, backlog must be allowed to grow through a burst
    // and drain later rather than be discarded. It only exists so a truly
    // pathological, sustained overload degrades (by dropping, loudly) rather
    // than taking the whole process down with it — which would lose
    // everything from that point on, not just the overflow. See --max-queued-events.
    explicit EventQueue(size_t max_queued_events) : max_queued_events_(max_queued_events) {}

    void push(const Prophesee::EventCD *begin, const Prophesee::EventCD *end) {
        std::vector<Prophesee::EventCD> batch(begin, end);
        std::lock_guard<std::mutex> lock(mutex_);
        queued_events_ += batch.size();
        batches_.push_back(std::move(batch));
        // Only hit in a genuinely pathological, sustained overload (sustained
        // production rate above what the link can ever drain, held long
        // enough to exhaust the configured budget) — everyday bursts just
        // make the backlog (and therefore display/recording lag) bigger,
        // which is fine; see kMaxQueuedEvents's doc comment. Dropping here
        // means real, permanent data loss, so it's logged loudly by the caller.
        while (max_queued_events_ > 0 && queued_events_ > max_queued_events_ && !batches_.empty()) {
            dropped_ += batches_.front().size();
            queued_events_ -= batches_.front().size();
            batches_.pop_front();
        }
        cv_.notify_one();
    }

    size_t queued_events() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return queued_events_;
    }

    // Waits up to `timeout` for a batch. kStopped is only returned once the
    // queue has been drained after stop() was called.
    PopResult pop(std::vector<Prophesee::EventCD> &out, std::chrono::milliseconds timeout) {
        std::unique_lock<std::mutex> lock(mutex_);
        if (!cv_.wait_for(lock, timeout, [this] { return !batches_.empty() || stopping_; }))
            return PopResult::kTimeout;
        if (batches_.empty())
            return PopResult::kStopped;
        out = std::move(batches_.front());
        batches_.pop_front();
        queued_events_ -= out.size();
        return PopResult::kGot;
    }

    // Non-blocking: pops one already-queued batch, if any, without waiting.
    // Used to opportunistically coalesce a backlog into fewer, larger sends.
    bool try_pop(std::vector<Prophesee::EventCD> &out) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (batches_.empty())
            return false;
        out = std::move(batches_.front());
        batches_.pop_front();
        queued_events_ -= out.size();
        return true;
    }

    // Drops any backlog — used when a new client connects, so it sees live
    // events rather than a burst of whatever queued up while no one was
    // listening.
    void clear() {
        std::lock_guard<std::mutex> lock(mutex_);
        batches_.clear();
        queued_events_ = 0;
    }

    void stop() {
        std::lock_guard<std::mutex> lock(mutex_);
        stopping_ = true;
        cv_.notify_all();
    }

    // Returns the number of events dropped (queue full) since the last call.
    uint64_t take_dropped() {
        return dropped_.exchange(0);
    }

private:
    const size_t max_queued_events_;
    mutable std::mutex mutex_;
    std::condition_variable cv_;
    std::deque<std::vector<Prophesee::EventCD>> batches_;
    size_t queued_events_ = 0;
    std::atomic<uint64_t> dropped_{0};
    bool stopping_ = false;
};

bool send_all(int fd, const void *data, size_t len) {
    const char *p = static_cast<const char *>(data);
    while (len > 0) {
        ssize_t n = ::send(fd, p, len, MSG_NOSIGNAL);
        if (n <= 0)
            return false;
        p += n;
        len -= static_cast<size_t>(n);
    }
    return true;
}

int make_listen_socket(const std::string &bind_addr, uint16_t port) {
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        std::perror("socket");
        return -1;
    }
    int opt = 1;
    ::setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    // Poll accept() with a timeout rather than blocking forever, so Ctrl+C
    // is noticed promptly even while no client has ever connected.
    timeval rcv_timeout{};
    rcv_timeout.tv_sec = 0;
    rcv_timeout.tv_usec = 500000;
    ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &rcv_timeout, sizeof(rcv_timeout));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    if (bind_addr.empty() || bind_addr == "0.0.0.0") {
        addr.sin_addr.s_addr = INADDR_ANY;
    } else if (::inet_pton(AF_INET, bind_addr.c_str(), &addr.sin_addr) != 1) {
        std::cerr << "Invalid --bind address: " << bind_addr << std::endl;
        ::close(fd);
        return -1;
    }

    if (::bind(fd, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) < 0) {
        std::perror("bind");
        ::close(fd);
        return -1;
    }
    if (::listen(fd, 1) < 0) {
        std::perror("listen");
        ::close(fd);
        return -1;
    }
    return fd;
}

} // namespace

int main(int argc, char *argv[]) {
    std::string bind_addr;
    uint16_t port = 0;
    std::string biases_file;
    std::string serial;
    uint32_t max_rate_kev_s = 0;
    uint32_t max_queued_events = 0;

    const std::string program_desc =
        "\nStreams CD events from a Prophesee Onboard camera over TCP, as a\n"
        "sequence of length-framed binary event batches — see the header\n"
        "comment in tcp_event_streamer.cpp for the wire format.\n\n"
        "Press Ctrl+C to stop.\n";

    po::options_description desc(program_desc + "\nAllowed options");
    // clang-format off
    desc.add_options()
        ("help,h", "Print this help message")
        ("port,p", po::value<uint16_t>(&port)->default_value(9000), "TCP port to listen on")
        ("bind", po::value<std::string>(&bind_addr)->default_value("0.0.0.0"), "Address to bind the listening socket to")
        ("biases,b", po::value<std::string>(&biases_file)->default_value(""), "Path to a biases file. If not specified, default biases are used.")
        ("serial,s", po::value<std::string>(&serial)->default_value(""), "Camera serial number (blank = first available)")
        ("max-rate", po::value<uint32_t>(&max_rate_kev_s)->default_value(0),
            "Cap event production at the sensor, in kilo-events/sec (0 = unlimited, "
            "the default). WARNING: events above this rate are never generated at "
            "all — this is permanent data loss, not lag. Only use this if you "
            "specifically want a thinned live preview and don't need every event; "
            "leave it at 0 for lossless/scientific capture.")
        ("max-queued-events", po::value<uint32_t>(&max_queued_events)->default_value(20000000),
            "OOM safety valve, not a lag control: events are never dropped to keep "
            "the stream 'live' — backlog is allowed to grow (and display/recording "
            "lag along with it) through bursts and drains later. This bound only "
            "exists so a sustained, indefinite overload degrades (drops, loudly) "
            "instead of exhausting memory and crashing the process, which would "
            "lose everything from that point on rather than just the overflow. "
            "The default is ~20M events (several hundred MB); raise it if you have "
            "RAM to spare and expect very long sustained bursts, or set 0 to disable "
            "the safety valve entirely (unbounded — risks an OOM crash instead of a "
            "bounded, logged data loss under pathological overload).")
    ;
    // clang-format on

    po::variables_map vm;
    try {
        po::store(po::command_line_parser(argc, argv).options(desc).run(), vm);
        po::notify(vm);
    } catch (std::exception &e) {
        std::cerr << e.what() << std::endl;
        std::cout << desc << std::endl;
        return 1;
    }
    if (vm.count("help")) {
        std::cout << desc << std::endl;
        return 0;
    }

    signal(SIGINT, sig_handler);
    signal(SIGTERM, sig_handler);
    signal(SIGPIPE, SIG_IGN); // sends use MSG_NOSIGNAL too, but belt-and-braces

    Prophesee::Camera camera;
    try {
        if (serial.empty()) {
            camera = Prophesee::Camera::from_first_available();
        } else {
            camera = Prophesee::Camera::from_serial(serial);
        }
        if (!biases_file.empty()) {
            camera.biases().set_from_file(biases_file);
        }
        if (max_rate_kev_s > 0) {
            camera.set_max_event_rate_limit(max_rate_kev_s);
            std::cerr << "Max event rate limited to " << max_rate_kev_s << " kEv/s" << std::endl;
        }
    } catch (Prophesee::CameraException &e) {
        std::cerr << "Failed to open camera: " << e.what() << std::endl;
        return 1;
    }

    auto &geometry = camera.geometry();
    std::cerr << "Camera opened: " << geometry.width() << "x" << geometry.height() << std::endl;

    int listen_fd = make_listen_socket(bind_addr, port);
    if (listen_fd < 0)
        return 1;
    std::cerr << "Listening on " << bind_addr << ":" << port << std::endl;

    EventQueue queue(max_queued_events);

    WireHeader header{};
    std::memcpy(header.magic, "PECD", 4);
    header.version = kProtocolVersion;
    header.width = static_cast<uint16_t>(geometry.width());
    header.height = static_cast<uint16_t>(geometry.height());

    // Not started yet: the camera's CD callback and camera.start() are
    // deferred to the first accepted connection (see below), instead of
    // running from process launch regardless of whether anyone is
    // listening. Pulling (and queuing) events before any client exists has
    // no recipient for that data and only grows an unbounded backlog for no
    // reason — camera.is_running() is therefore not a valid loop condition
    // until after that first start(), hence camera_started below.
    bool camera_started = false;

    while (!g_signal_caught && (!camera_started || camera.is_running())) {
        sockaddr_in client_addr{};
        socklen_t client_len = sizeof(client_addr);
        int client_fd = ::accept(listen_fd, reinterpret_cast<sockaddr *>(&client_addr), &client_len);
        if (client_fd < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)
                continue; // accept() timeout (SO_RCVTIMEO) — recheck signal flag and retry
            std::perror("accept");
            continue;
        }
        int nodelay = 1;
        ::setsockopt(client_fd, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));

        if (!camera_started) {
            camera.cd().add_callback([&queue](const Prophesee::EventCD *begin, const Prophesee::EventCD *end) {
                queue.push(begin, end);
            });
            camera.add_runtime_error_callback(
                [](const Prophesee::CameraException &e) { std::cerr << "Runtime error: " << e.what() << std::endl; });
            camera.start();
            camera_started = true;
            std::cerr << "First client connected — camera started." << std::endl;
        }

        char client_ip[INET_ADDRSTRLEN];
        ::inet_ntop(AF_INET, &client_addr.sin_addr, client_ip, sizeof(client_ip));
        size_t backlog = queue.queued_events();
        std::cerr << "Client connected: " << client_ip;
        if (backlog > 0)
            std::cerr << "  (" << backlog << " backlogged events queued since the last "
                                              "connection — delivering those first, oldest first)";
        std::cerr << std::endl;
        uint64_t dropped = queue.take_dropped();
        if (dropped > 0)
            std::cerr << "  (" << dropped << " events LOST while no client was connected — "
                                              "hit --max-queued-events; raise it if this recurs)"
                      << std::endl;

        if (!send_all(client_fd, &header, sizeof(header))) {
            std::cerr << "Failed to send handshake — dropping client." << std::endl;
            ::close(client_fd);
            continue;
        }

        // Deliberately NOT clearing the queue here: any backlog that built up
        // while no client was connected (e.g. a dropped/reconnecting TCP
        // session) gets delivered to this client, oldest-first, rather than
        // silently discarded. Losing data on a reconnect would defeat the
        // whole point of buffering through gaps instead of dropping.
        std::vector<Prophesee::EventCD> batch, extra;
        std::vector<char> send_buf;
        bool client_ok = true;
        auto last_status_report = std::chrono::steady_clock::now();

        while (client_ok && !g_signal_caught && camera.is_running()) {
            auto now = std::chrono::steady_clock::now();
            if (now - last_status_report >= std::chrono::seconds(10)) {
                last_status_report = now;
                // Backlog depth is the honest measure of "how far behind is this
                // session" for a lossless capture — unlike a drop count (which
                // should normally stay at 0), a growing backlog is expected and
                // fine, but worth surfacing so you can tell a burst from a
                // sustained overload while it's still recording.
                std::cerr << "  (status: " << queue.queued_events() << " events queued)" << std::endl;
            }
            uint64_t dropped_now = queue.take_dropped();
            if (dropped_now > 0)
                std::cerr << "  (DATA LOSS: " << dropped_now
                          << " events dropped — sustained overload exceeded --max-queued-events; "
                             "raise it, or reduce sensor activity, if this recurs)"
                          << std::endl;

            PopResult result = queue.pop(batch, std::chrono::milliseconds(200));
            if (result == PopResult::kStopped)
                break;
            if (result == PopResult::kTimeout)
                continue;

            // Opportunistically coalesce whatever else is already queued —
            // this never waits any longer for more data, it just picks up a
            // backlog in one shot instead of one small message per camera
            // callback. Cuts syscall/packet count on the sender (matters on
            // the Onboard's weak ARM CPU) and per-message processing
            // overhead on the receiver (network_reader.py) alike, without
            // adding latency.
            while (batch.size() < kMaxEventsPerMessage && queue.try_pop(extra)) {
                batch.insert(batch.end(), extra.begin(), extra.end());
            }
            if (batch.empty())
                continue;

            uint32_t count = static_cast<uint32_t>(batch.size());
            send_buf.resize(sizeof(count) + batch.size() * sizeof(WireEvent));
            std::memcpy(send_buf.data(), &count, sizeof(count));

            WireEvent *wire = reinterpret_cast<WireEvent *>(send_buf.data() + sizeof(count));
            for (size_t i = 0; i < batch.size(); ++i) {
                wire[i].x = static_cast<uint16_t>(batch[i].x);
                wire[i].y = static_cast<uint16_t>(batch[i].y);
                wire[i].p = static_cast<int16_t>(batch[i].p);
                wire[i]._reserved = 0;
                wire[i].t = static_cast<int64_t>(batch[i].t);
            }

            client_ok = send_all(client_fd, send_buf.data(), send_buf.size());
        }

        std::cerr << "Client disconnected." << std::endl;
        ::close(client_fd);
    }

    std::cerr << "Shutting down..." << std::endl;
    queue.stop();
    if (camera_started)
        camera.stop();
    ::close(listen_fd);
    return 0;
}
