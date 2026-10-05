/************************************************************************
 * File : genx320_streamer.cpp                                          *
 *                                                                      *
 * Streams decoded CD events from a GenX320 (or any OpenEB-supported    *
 * camera) over a raw TCP socket, for consumption by network_reader.py's*
 * NetworkEventsIterator (event-camera-viewer) — same wire protocol as  *
 * onboard_streamer/tcp_event_streamer.cpp, same architecture, but      *
 * built against OpenEB's own open-source C++ SDK (metavision_sdk_stream*
 * + metavision_hal) instead of the closed-source prophesee_driver SDK  *
 * the Onboard tool links against, since a GenX320 build has no such    *
 * closed SDK to link against in the first place.                      *
 *                                                                      *
 * Why this exists alongside genx320_streamer.py (same job, in Python): *
 * profiling genx320_streamer.py under real load showed ~100% CPU spent*
 * inside metavision_core's own native EVT-format decode call, with the *
 * GIL serializing it against the send loop on a single core the whole  *
 * time — so the send loop couldn't drain the queue even though decode  *
 * itself wasn't literally too slow in absolute terms. A C++ build pays *
 * the same native decode cost (it's the same underlying SDK call       *
 * either way) but the camera's callback thread and the network-send    *
 * thread are genuine OS threads with no GIL between them, free to run  *
 * on separate cores of a multi-core Pi concurrently.                   *
 *                                                                      *
 * No display/GUI — safe to run headless over SSH.                      *
 *                                                                      *
 * Wire protocol: identical to onboard_streamer/tcp_event_streamer.cpp  *
 * — see its header comment for the authoritative byte-for-byte spec.   *
 * network_reader.py's _DecodedNetworkEventsIterator needs no changes   *
 * to talk to this tool instead of that one.                            *
 *                                                                      *
 * Beyond the wire protocol, this also ports two behaviors from the     *
 * current genx320_streamer.py (added after tcp_event_streamer.cpp was  *
 * originally written, see that script's own history/comments for the   *
 * incidents that motivated them) that tcp_event_streamer.cpp does NOT  *
 * have, because its lossless-capture-always use case never needed them:*
 *   - Lazy start: the camera isn't opened/started until the first      *
 *     client connects, instead of running (and queuing backlog) from   *
 *     process launch regardless of whether anyone is listening.        *
 *   - A live-view/recording distinction, toggled by a 1-byte command   *
 *     the client sends (see network_reader.py's set_recording()): live *
 *     view defaults to a small cap that drops old backlog to stay near *
 *     real-time, and discards backlog on a reconnect; telling it you're*
 *     recording switches to a much larger (default: unlimited) cap and *
 *     preserves backlog across a reconnect instead, so a brief network *
 *     hiccup mid-recording doesn't lose data.                          *
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
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <boost/program_options.hpp>

#include <metavision/sdk/stream/camera.h>
#include <metavision/hal/facilities/i_erc_module.h>

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
// and t that gives Metavision::EventCD's own layout its 8-byte alignment
// for t — always written as 0.
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

// Client -> server commands (the only bytes ever sent the other way on this
// connection) — see network_reader.py's set_recording(), the only sender.
constexpr char kCmdStopRecording = 0x00;
constexpr char kCmdStartRecording = 0x01;

enum class PopResult { kGot, kTimeout, kStopped };

/// Thread-safe hand-off between the camera's CD callback (producer, called
/// on the driver's own thread) and the socket-writer thread (consumer).
/// Enforces one of two caps depending on recording_active (shared with the
/// command-reading thread): see the module header comment above for the
/// live-view/recording rationale this mirrors from genx320_streamer.py.
class EventQueue {
public:
    EventQueue(size_t live_cap, size_t recording_cap, const std::atomic<bool> &recording_active) :
        live_cap_(live_cap), recording_cap_(recording_cap), recording_active_(recording_active) {}

    void push(const Metavision::EventCD *begin, const Metavision::EventCD *end) {
        std::vector<Metavision::EventCD> batch(begin, end);
        std::lock_guard<std::mutex> lock(mutex_);
        queued_events_ += batch.size();
        batches_.push_back(std::move(batch));
        size_t cap = recording_active_.load() ? recording_cap_ : live_cap_;
        while (cap > 0 && queued_events_ > cap && !batches_.empty()) {
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

    // Discards any queued backlog and returns how many events were dropped.
    // Used when a new client connects while NOT recording — see the call
    // site in main() for why that's the opposite default from
    // tcp_event_streamer.cpp, which is built for always-lossless capture.
    size_t clear() {
        std::lock_guard<std::mutex> lock(mutex_);
        size_t dropped = queued_events_;
        batches_.clear();
        queued_events_ = 0;
        return dropped;
    }

    PopResult pop(std::vector<Metavision::EventCD> &out, std::chrono::milliseconds timeout) {
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

    bool try_pop(std::vector<Metavision::EventCD> &out) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (batches_.empty())
            return false;
        out = std::move(batches_.front());
        batches_.pop_front();
        queued_events_ -= out.size();
        return true;
    }

    void stop() {
        std::lock_guard<std::mutex> lock(mutex_);
        stopping_ = true;
        cv_.notify_all();
    }

    uint64_t take_dropped() {
        return dropped_.exchange(0);
    }

private:
    const size_t live_cap_;
    const size_t recording_cap_;
    const std::atomic<bool> &recording_active_;
    mutable std::mutex mutex_;
    std::condition_variable cv_;
    std::deque<std::vector<Metavision::EventCD>> batches_;
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

/// Runs on its own thread for the life of one connection, reading 1-byte
/// commands from the client — see network_reader.py's set_recording(), the
/// only sender, and EventQueue's docstring-equivalent comment above for
/// what recording_active controls. recording_active is process-level, not
/// per-connection: once the client says it's recording, backlog keeps
/// getting preserved across any later disconnect/reconnect (see main())
/// until it explicitly says to stop — a dropped connection alone is not
/// distinguished from a deliberate pause, since losing data mid-recording
/// is the wrong default either way.
void read_commands(int client_fd, std::atomic<bool> &recording_active) {
    char cmd;
    while (true) {
        ssize_t n = ::recv(client_fd, &cmd, 1, 0);
        if (n <= 0)
            return;
        if (cmd == kCmdStartRecording) {
            recording_active = true;
            std::cerr << "  Recording started by client — switching to lossless capture "
                        "(backlog preserved across reconnects)." << std::endl;
        } else if (cmd == kCmdStopRecording) {
            recording_active = false;
            std::cerr << "  Recording stopped by client — back to live view "
                        "(backlog discarded on reconnect)." << std::endl;
        }
    }
}

} // namespace

int main(int argc, char *argv[]) {
    std::string bind_addr;
    uint16_t port = 0;
    std::string serial;
    uint32_t max_rate_kev_s = 0;
    uint32_t live_queue_cap = 0;
    uint32_t max_queued_events = 0;

    const std::string program_desc =
        "\nStreams decoded CD events from a GenX320 (or any OpenEB-supported\n"
        "camera) over TCP — see the header comment in genx320_streamer.cpp for\n"
        "the wire format (identical to onboard_streamer/tcp_event_streamer.cpp)\n"
        "and the live-view/recording distinction this adds on top of it.\n\n"
        "Press Ctrl+C to stop.\n";

    po::options_description desc(program_desc + "\nAllowed options");
    // clang-format off
    desc.add_options()
        ("help,h", "Print this help message")
        ("port,p", po::value<uint16_t>(&port)->default_value(9000), "TCP port to listen on")
        ("bind", po::value<std::string>(&bind_addr)->default_value("0.0.0.0"), "Address to bind the listening socket to")
        ("serial,s", po::value<std::string>(&serial)->default_value(""), "Camera serial number (blank = first available)")
        ("max-rate", po::value<uint32_t>(&max_rate_kev_s)->default_value(0),
            "Cap event production at the sensor, in kilo-events/sec (0 = unlimited, "
            "the default), via the camera's on-chip Event Rate Controller (ERC) if "
            "it has one. WARNING: events above this rate are never generated at "
            "all — permanent data loss at the source, not lag. The right tool when "
            "the camera can sustainably produce more events than the network link "
            "can carry.")
        ("live-queue-cap", po::value<uint32_t>(&live_queue_cap)->default_value(500000),
            "Cap on queued-but-unsent events while NOT recording (default: 500,000). "
            "Deliberately small: if the camera outpaces the link, this keeps live-view "
            "lag bounded by dropping old backlog promptly instead of growing it "
            "unboundedly. 0 disables the cap (unbounded even while just viewing).")
        ("max-queued-events", po::value<uint32_t>(&max_queued_events)->default_value(0),
            "Cap on queued-but-unsent events while RECORDING (default: 0 = unlimited — "
            "every event is kept and eventually delivered no matter how large the "
            "backlog grows, because recording means you need all of it). This trades "
            "away OOM protection: a nonzero value turns it into a circuit breaker "
            "instead (dropping, loudly, rather than growing memory without bound).")
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

    int listen_fd = make_listen_socket(bind_addr, port);
    if (listen_fd < 0)
        return 1;
    std::cerr << "Listening on " << bind_addr << ":" << port << std::endl;
    std::cerr << "Waiting for a client before opening the camera …" << std::endl;

    std::atomic<bool> recording_active(false);
    EventQueue queue(live_queue_cap, max_queued_events, recording_active);

    // Not opened/started until the first client connects — see the module
    // header comment above for why.
    std::unique_ptr<Metavision::Camera> camera;
    bool camera_started = false;
    WireHeader header{};

    while (!g_signal_caught && (!camera_started || camera->is_running())) {
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
            try {
                camera = std::make_unique<Metavision::Camera>(
                    serial.empty() ? Metavision::Camera::from_first_available() : Metavision::Camera::from_serial(serial));
            } catch (Metavision::CameraException &e) {
                std::cerr << "Failed to open camera: " << e.what() << std::endl;
                ::close(client_fd);
                ::close(listen_fd);
                return 1;
            }

            if (max_rate_kev_s > 0) {
                auto *erc = camera->get_device().get_facility<Metavision::I_ErcModule>();
                if (erc && erc->set_cd_event_rate(max_rate_kev_s * 1000) && erc->enable(true)) {
                    std::cerr << "Max event rate limited to " << max_rate_kev_s << " kEv/s (on-chip ERC)" << std::endl;
                } else {
                    std::cerr << "[WARN] --max-rate requested but this camera has no usable ERC facility — "
                                "ignoring, event rate is unlimited." << std::endl;
                }
            }

            auto &geometry = camera->geometry();
            std::cerr << "Camera opened: " << geometry.get_width() << "x" << geometry.get_height() << std::endl;
            std::memcpy(header.magic, "PECD", 4);
            header.version = kProtocolVersion;
            header.width = static_cast<uint16_t>(geometry.get_width());
            header.height = static_cast<uint16_t>(geometry.get_height());

            camera->cd().add_callback(
                [&queue](const Metavision::EventCD *begin, const Metavision::EventCD *end) { queue.push(begin, end); });
            camera->add_runtime_error_callback(
                [](const Metavision::CameraException &e) { std::cerr << "Runtime error: " << e.what() << std::endl; });
            camera->start();
            camera_started = true;
            std::cerr << "First client connected — camera started." << std::endl;
        }

        // Discard any backlog that piled up while no client was connected —
        // including before the very first connection — UNLESS the client
        // told us (via a kCmdStartRecording byte) it's actively recording,
        // in which case this is a reconnect mid-recording and the backlog
        // is preserved instead (bounded only by --max-queued-events). See
        // the module header comment above for the full rationale.
        char client_ip[INET_ADDRSTRLEN];
        ::inet_ntop(AF_INET, &client_addr.sin_addr, client_ip, sizeof(client_ip));
        if (recording_active.load()) {
            std::cerr << "Client connected: " << client_ip << "  (recording still active — backlog preserved)"
                      << std::endl;
        } else {
            size_t stale = queue.clear();
            queue.take_dropped();
            std::cerr << "Client connected: " << client_ip;
            if (stale > 0)
                std::cerr << "  (discarded " << stale << " stale backlogged events — starting live)";
            std::cerr << std::endl;
        }

        if (!send_all(client_fd, &header, sizeof(header))) {
            std::cerr << "Failed to send handshake — dropping client." << std::endl;
            ::close(client_fd);
            continue;
        }

        std::thread cmd_thread(read_commands, client_fd, std::ref(recording_active));

        std::vector<Metavision::EventCD> batch, extra;
        std::vector<char> send_buf;
        bool client_ok = true;
        auto last_status_report = std::chrono::steady_clock::now();

        while (client_ok && !g_signal_caught && camera->is_running()) {
            auto now = std::chrono::steady_clock::now();
            if (now - last_status_report >= std::chrono::seconds(10)) {
                last_status_report = now;
                std::cerr << "  (status: " << queue.queued_events() << " events queued)" << std::endl;
            }
            uint64_t dropped_now = queue.take_dropped();
            if (dropped_now > 0) {
                if (recording_active.load()) {
                    std::cerr << "  (DATA LOSS: " << dropped_now
                              << " events dropped — sustained overload exceeded --max-queued-events; "
                                 "raise it, or reduce sensor activity, if this recurs)"
                              << std::endl;
                } else {
                    std::cerr << "  (falling behind: " << dropped_now
                              << " oldest events dropped to stay live — not recording, so this is "
                                 "expected under sustained overload; see --max-rate to cap it at the source)"
                              << std::endl;
                }
            }

            PopResult result = queue.pop(batch, std::chrono::milliseconds(200));
            if (result == PopResult::kStopped)
                break;
            if (result == PopResult::kTimeout)
                continue;

            // Opportunistically coalesce whatever else is already queued —
            // never waits any longer for more data, just picks up a
            // backlog in one shot instead of one small message per camera
            // callback. Cuts syscall/packet count on the sender and
            // per-message processing overhead on the receiver alike.
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
        ::shutdown(client_fd, SHUT_RDWR); // unblocks read_commands()'s recv()
        cmd_thread.join();
        ::close(client_fd);
    }

    std::cerr << "Shutting down..." << std::endl;
    queue.stop();
    if (camera_started)
        camera->stop();
    ::close(listen_fd);
    return 0;
}
