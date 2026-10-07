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
 *       uint32_t version   protocol version (currently 2)              *
 *       uint16_t width                                                 *
 *       uint16_t height                                                *
 *                                                                      *
 *   Then, repeated for the life of the connection:                     *
 *       uint32_t count               number of events in this batch    *
 *       count * WireEvent            16 bytes each, see below          *
 *                                                                      *
 *   Client -> server commands:                                         *
 *       0x00                         back to live view (the default)   *
 *       0x01                         client is recording the stream    *
 *       0x02, uint8_t len, name      start RAW recording as <name>     *
 *       0x03                         stop it and send the file back    *
 *                                                                      *
 *   Server -> client control message, sent in place of an event batch: *
 *       uint32_t 0xFFFFFFFF          marker (never a valid count)      *
 *       uint8_t  type                see ControlType below             *
 *       uint32_t len                 payload length in bytes           *
 *       len bytes of payload                                           *
 *                                                                      *
 * Behavior shared with genx320_streamer.py and genx320_streamer_native *
 * (all three are kept functionally identical — change them together): *
 *   - Lazy start: the camera isn't started until the first client      *
 *     connects, instead of queuing backlog from process launch.        *
 *   - A live-view/recording distinction, toggled by the 0x00/0x01      *
 *     commands (see network_reader.py's set_recording()): live view    *
 *     defaults to a small cap that drops old backlog to stay near      *
 *     real-time, and discards backlog on a reconnect; telling it you're*
 *     recording switches to a much larger (default: unlimited) cap and *
 *     preserves backlog across a reconnect instead, so a brief network *
 *     hiccup mid-recording doesn't lose data.                          *
 *   - Remote RAW recording (protocol version 2): on request the camera *
 *     records undecoded data to a file on this device (--record-dir)   *
 *     while the stream carries on as a preview, and the file is sent   *
 *     back over the same connection when recording stops.              *
 *                                                                      *
 * Based on Prophesee driver samples, Copyright (c) 2018 Prophesee      *
 ************************************************************************/

#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <sys/stat.h>
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
#include <functional>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
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

constexpr uint32_t kProtocolVersion = 2;
// Cap on how much backlog gets coalesced into a single network message (see
// the send loop in main): bounds worst-case message size (~3.2MB) without
// meaningfully limiting how much a burst can be coalesced.
constexpr size_t kMaxEventsPerMessage = 200000;

// Client -> server commands (the only bytes ever sent the other way on this
// connection) — see network_reader.py, the only sender.
constexpr char kCmdStopRecording = 0x00;  // back to live view (small queue cap)
constexpr char kCmdStartRecording = 0x01; // client is recording the stream: lossless queue
constexpr char kCmdStartRaw = 0x02;       // + uint8_t len, name: record RAW on this device
constexpr char kCmdStopRaw = 0x03;        // stop that and send the file back

// Server -> client control message, sent in place of an event batch: this
// marker where the count would be, then uint8_t type, uint32_t payload
// length, payload.
constexpr uint32_t kControlMarker = 0xFFFFFFFF;
enum class ControlType : uint8_t {
    kRecordingStarted = 1, // no payload
    kRecordingError = 2,   // payload: human-readable message
    kFileBegin = 3,        // payload: uint64_t file size in bytes
    kFileData = 4,         // payload: next chunk of the RAW file
    kFileEnd = 5,          // no payload
};
// One chunk of a finished RAW file is sent per event message, so the live
// stream keeps flowing (and the queue keeps draining) during the transfer.
constexpr size_t kFileChunkBytes = 1 << 20;

enum class PopResult { kGot, kTimeout, kStopped };

/// Thread-safe hand-off between the camera's CD callback (producer, called
/// on the driver's own thread) and the socket-writer thread (consumer).
/// Enforces one of two caps depending on recording_active (shared with the
/// command-reading thread): see the header comment above.
class EventQueue {
public:
    EventQueue(size_t live_cap, size_t recording_cap, const std::atomic<bool> &recording_active) :
        live_cap_(live_cap), recording_cap_(recording_cap), recording_active_(recording_active) {}

    void push(const Prophesee::EventCD *begin, const Prophesee::EventCD *end) {
        std::vector<Prophesee::EventCD> batch(begin, end);
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
    // site in main().
    size_t clear() {
        std::lock_guard<std::mutex> lock(mutex_);
        size_t dropped = queued_events_;
        batches_.clear();
        queued_events_ = 0;
        return dropped;
    }

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

    bool try_pop(std::vector<Prophesee::EventCD> &out) {
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

bool send_control(int fd, ControlType type, const void *payload = nullptr, uint32_t len = 0) {
    char hdr[sizeof(kControlMarker) + sizeof(uint8_t) + sizeof(len)];
    std::memcpy(hdr, &kControlMarker, sizeof(kControlMarker));
    hdr[sizeof(kControlMarker)] = static_cast<char>(type);
    std::memcpy(hdr + sizeof(kControlMarker) + 1, &len, sizeof(len));
    return send_all(fd, hdr, sizeof(hdr)) && (len == 0 || send_all(fd, payload, len));
}

bool send_control(int fd, ControlType type, const std::string &text) {
    return send_control(fd, type, text.data(), static_cast<uint32_t>(text.size()));
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

/// RAW recording requests, handed from the command-reading thread to the
/// send loop — the only thread that touches the camera's recording API or
/// writes to the socket.
class RawRequests {
public:
    void post(bool start, const std::string &name = std::string()) {
        std::lock_guard<std::mutex> lock(mutex_);
        requests_.emplace_back(start, name);
    }

    bool take(bool &start, std::string &name) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (requests_.empty())
            return false;
        start = requests_.front().first;
        name = requests_.front().second;
        requests_.pop_front();
        return true;
    }

private:
    std::mutex mutex_;
    std::deque<std::pair<bool, std::string>> requests_;
};

/// Remote RAW recording (protocol version 2): records undecoded sensor data
/// to a file in `dir` on this device using the camera's own RAW recorder,
/// while the decoded stream carries on as a live preview, then sends the
/// file back over the same connection once recording stops and deletes it.
/// A file the client hasn't fully received is never deleted.
class RawRecorder {
public:
    using StartFn = std::function<std::string(const std::string &path)>; // returns an error message, empty if OK
    using StopFn = std::function<void()>;

    RawRecorder(const std::string &dir, StartFn start, StopFn stop) : dir_(dir), start_(start), stop_(stop) {}

    bool transferring() const {
        return file_fd_ >= 0;
    }

    /// Acts on any pending requests, then sends the next chunk of a finished
    /// recording, if there is one. Returns false if the connection failed.
    bool service(int client_fd, RawRequests &requests) {
        bool start = false;
        std::string name;
        while (requests.take(start, name)) {
            if (!(start ? handle_start(client_fd, name) : handle_stop(client_fd)))
                return false;
        }
        return send_next_chunk(client_fd);
    }

    void client_disconnected() {
        if (!rec_path_.empty()) {
            stop_();
            std::cerr << "Client left mid-recording — RAW file kept on this device: " << rec_path_ << std::endl;
            rec_path_.clear();
        }
        if (file_fd_ >= 0) {
            ::close(file_fd_);
            file_fd_ = -1;
            std::cerr << "Transfer interrupted — RAW file kept on this device: " << file_path_ << std::endl;
        }
    }

private:
    bool handle_start(int client_fd, const std::string &name) {
        std::string error;
        if (!rec_path_.empty() || file_fd_ >= 0) {
            error = "a recording is already in progress or still being transferred";
        } else if (name.empty() || name[0] == '.' || name.find('/') != std::string::npos) {
            error = "invalid recording name";
        } else {
            const std::string path = dir_ + "/" + name;
            error = start_(path);
            if (error.empty()) {
                rec_path_ = path;
                std::cerr << "RAW recording started: " << rec_path_ << std::endl;
            }
        }
        return error.empty() ? send_control(client_fd, ControlType::kRecordingStarted)
                             : send_control(client_fd, ControlType::kRecordingError, error);
    }

    bool handle_stop(int client_fd) {
        if (rec_path_.empty())
            return true;
        stop_();
        file_path_ = rec_path_;
        rec_path_.clear();
        struct stat st {};
        file_fd_ = ::open(file_path_.c_str(), O_RDONLY);
        if (file_fd_ < 0 || ::fstat(file_fd_, &st) < 0) {
            if (file_fd_ >= 0)
                ::close(file_fd_);
            file_fd_ = -1;
            return send_control(client_fd, ControlType::kRecordingError,
                                "no RAW file was written at " + file_path_ + " (is --record-dir writable?)");
        }
        const uint64_t size = static_cast<uint64_t>(st.st_size);
        std::cerr << "RAW recording stopped: sending " << file_path_ << " (" << size << " bytes)" << std::endl;
        return send_control(client_fd, ControlType::kFileBegin, &size, sizeof(size));
    }

    bool send_next_chunk(int client_fd) {
        if (file_fd_ < 0)
            return true;
        chunk_.resize(kFileChunkBytes);
        const ssize_t got = ::read(file_fd_, chunk_.data(), chunk_.size());
        if (got > 0)
            return send_control(client_fd, ControlType::kFileData, chunk_.data(), static_cast<uint32_t>(got));
        if (got == 0) {
            // file_fd_ is left open on failure, so client_disconnected() reports the file as kept.
            if (!send_control(client_fd, ControlType::kFileEnd))
                return false;
            ::close(file_fd_);
            file_fd_ = -1;
            ::unlink(file_path_.c_str());
            std::cerr << "RAW recording sent and removed from this device." << std::endl;
            return true;
        }
        ::close(file_fd_);
        file_fd_ = -1;
        std::cerr << "Reading " << file_path_ << " failed — file kept on this device." << std::endl;
        return send_control(client_fd, ControlType::kRecordingError,
                            "reading the RAW file failed on the device; it was kept at " + file_path_);
    }

    const std::string dir_;
    const StartFn start_;
    const StopFn stop_;
    std::string rec_path_;  // set while the camera is recording to it
    std::string file_path_; // the finished recording being sent back
    int file_fd_ = -1;      // open while that transfer is in progress
    std::vector<char> chunk_;
};

/// Runs on its own thread for the life of one connection, reading commands
/// from the client — see network_reader.py, the only sender. recording_active
/// is process-level, not per-connection: once the client says it's
/// recording, backlog keeps getting preserved across any later
/// disconnect/reconnect (see main()) until it explicitly says to stop — a
/// dropped connection alone is not distinguished from a deliberate pause,
/// since losing data mid-recording is the wrong default either way. RAW
/// recording requests are only parsed here; the send loop carries them out.
void read_commands(int client_fd, std::atomic<bool> &recording_active, RawRequests &raw_requests,
                   std::atomic<bool> &client_gone) {
    std::string buf;
    char tmp[256];
    while (true) {
        ssize_t n = ::recv(client_fd, tmp, sizeof(tmp), 0);
        if (n <= 0) {
            client_gone = true;
            return;
        }
        buf.append(tmp, static_cast<size_t>(n));
        while (!buf.empty()) {
            const char cmd = buf[0];
            if (cmd == kCmdStartRaw) {
                if (buf.size() < 2)
                    break; // rest of the command hasn't arrived yet
                const size_t name_len = static_cast<uint8_t>(buf[1]);
                if (buf.size() < 2 + name_len)
                    break;
                raw_requests.post(true, buf.substr(2, name_len));
                buf.erase(0, 2 + name_len);
                continue;
            }
            if (cmd == kCmdStartRecording) {
                recording_active = true;
                std::cerr << "  Recording started by client — switching to lossless capture "
                             "(backlog preserved across reconnects)." << std::endl;
            } else if (cmd == kCmdStopRecording) {
                recording_active = false;
                std::cerr << "  Recording stopped by client — back to live view "
                             "(backlog discarded on reconnect)." << std::endl;
            } else if (cmd == kCmdStopRaw) {
                raw_requests.post(false);
            }
            buf.erase(0, 1);
        }
    }
}

} // namespace

int main(int argc, char *argv[]) {
    std::string bind_addr;
    uint16_t port = 0;
    std::string biases_file;
    std::string serial;
    std::string record_dir;
    uint32_t max_rate_kev_s = 0;
    uint32_t live_queue_cap = 0;
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
        ("record-dir", po::value<std::string>(&record_dir)->default_value("/tmp"),
            "Directory RAW recordings requested by the client are written to on this "
            "device, before being sent back and deleted. Needs room for a whole "
            "recording and must keep up with the sensor's raw data rate.")
        ("max-rate", po::value<uint32_t>(&max_rate_kev_s)->default_value(0),
            "Cap event production at the sensor, in kilo-events/sec (0 = unlimited, "
            "the default). WARNING: events above this rate are never generated at "
            "all — this is permanent data loss, not lag. Only use this if you "
            "specifically want a thinned live preview and don't need every event; "
            "leave it at 0 for lossless/scientific capture.")
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

    std::atomic<bool> recording_active(false);
    EventQueue queue(live_queue_cap, max_queued_events, recording_active);

    RawRecorder raw_recorder(
        record_dir,
        [&camera](const std::string &path) -> std::string {
            try {
                camera.start_recording(path);
            } catch (Prophesee::CameraException &e) { return e.what(); }
            return std::string();
        },
        [&camera]() {
            try {
                camera.stop_recording();
            } catch (Prophesee::CameraException &e) { std::cerr << "stop_recording failed: " << e.what() << std::endl; }
        });

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

        // Discard any backlog that piled up while no client was connected
        // UNLESS the client told us (via a kCmdStartRecording byte) it's
        // actively recording, in which case this is a reconnect
        // mid-recording and the backlog is preserved instead (bounded only
        // by --max-queued-events).
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

        RawRequests raw_requests;
        std::atomic<bool> client_gone(false);
        std::thread cmd_thread(read_commands, client_fd, std::ref(recording_active), std::ref(raw_requests),
                               std::ref(client_gone));

        std::vector<Prophesee::EventCD> batch, extra;
        std::vector<char> send_buf;
        bool client_ok = true;
        auto last_status_report = std::chrono::steady_clock::now();

        while (client_ok && !client_gone && !g_signal_caught && camera.is_running()) {
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

            client_ok = raw_recorder.service(client_fd, raw_requests);
            if (!client_ok)
                break;

            // No waiting for events while a file transfer is in progress.
            PopResult result = queue.pop(batch, std::chrono::milliseconds(raw_recorder.transferring() ? 0 : 200));
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

        raw_recorder.client_disconnected();
        std::cerr << "Client disconnected." << std::endl;
        ::shutdown(client_fd, SHUT_RDWR); // unblocks read_commands()'s recv()
        cmd_thread.join();
        ::close(client_fd);
    }

    std::cerr << "Shutting down..." << std::endl;
    queue.stop();
    if (camera_started)
        camera.stop();
    ::close(listen_fd);
    return 0;
}
