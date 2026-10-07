"""Read CD events live from a camera streamed over TCP — a Prophesee Onboard
running tcp_event_streamer, or a GenX320 running genx320_streamer.py
(Python) or genx320_streamer_native (C++) — all three speak the identical
wire protocol below, so this client needs no knowledge of which one is on
the other end.

Wire protocol (all integers little-endian — both an embedded ARM streamer
and this viewer's x86 Linux host are little-endian, so no byte-swapping is
done). See the header comment in onboard_streamer/tcp_event_streamer.cpp
for the authoritative definition:

    Handshake, sent once right after the TCP connection is accepted:
        4s   magic     b"PECD"
        u32  version   protocol version (1, or 2 — see below)
        u16  width
        u16  height

    Then, repeated for the life of the connection:
        u32  count               number of events in this batch
        count * 16-byte events   see EVENT_CD_DTYPE (hdf5_reader.py) —
                                  x:u2, y:u2, p:i2, t:i8, with 2 bytes of
                                  padding between p and t

Protocol version 2 adds remote RAW recording, identically on all three
streamers: the device records undecoded sensor data to a file on its own storage while
the decoded stream above carries on as a live preview, then sends that file
back over this same connection once recording stops. See
start_remote_raw()/stop_remote_raw() below, and that file's header comment
for the command and control-message formats.

(An earlier --raw mode here relayed undecoded bytes for this module to
decode locally via OpenEB's RAW-file machinery, to move decode CPU cost off
a constrained Pi — abandoned after testing confirmed that machinery
unconditionally seeks to end-of-file to measure total size, which a live
stream can never support. See genx320_streamer_native/ for how that CPU
cost is actually addressed instead: the same wire protocol as here, just
produced in C++ rather than Python, so the camera's native-code callback
thread and this protocol's network-send thread aren't serialized by a GIL.)
"""
from __future__ import annotations

import os
import socket
import struct
import threading

import numpy as np

from hdf5_reader import EVENT_CD_DTYPE

_HEADER_FMT = "<4sIHH"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)
_MAGIC = b"PECD"
_PROTOCOL_VERSIONS = (1, 2)
_REMOTE_RAW_MIN_VERSION = 2
_COUNT_FMT = "<I"
_COUNT_SIZE = struct.calcsize(_COUNT_FMT)

_CONNECT_TIMEOUT_S = 10.0

# Sanity cap on a single batch's event count — comfortably above both
# tcp_event_streamer.cpp's and genx320_streamer.py's own coalescing cap
# (200,000), so legitimate traffic never trips it. A random/garbage count
# (e.g. from a desynced stream after a framing bug) is overwhelmingly likely
# to exceed this, so treating it as an error here — rather than trying to
# recv() and decode whatever it implies — turns silent data corruption that
# would otherwise reach the SDK's frame-generation code (which assumes
# well-formed, time-sorted input and does not itself validate it) into a
# clear, early Python exception instead of undefined native-code behavior.
_MAX_EVENTS_PER_BATCH = 2_000_000

# Client → server commands (the only bytes ever sent the other way on this
# connection). A server starts in a discard-backlog-on-reconnect "live view"
# mode, and these switch it to/from a lossless mode that preserves backlog
# across a reconnect instead, for the duration of an actual recording.
_CMD_STOP_RECORDING = b"\x00"
_CMD_START_RECORDING = b"\x01"
# Remote RAW recording (protocol version 2).
_CMD_START_RAW = b"\x02"  # followed by u8 name length, then the file name
_CMD_STOP_RAW = b"\x03"

# Server → client control message, sent in place of an event batch: this
# marker where the count would be, then u8 type, u32 payload length, payload.
_CONTROL_MARKER = 0xFFFFFFFF
_CONTROL_FMT = "<BI"
_CONTROL_SIZE = struct.calcsize(_CONTROL_FMT)
_CTRL_RECORDING_STARTED = 1
_CTRL_RECORDING_ERROR = 2
_CTRL_FILE_BEGIN = 3
_CTRL_FILE_DATA = 4
_CTRL_FILE_END = 5


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Block until exactly n bytes have been read (recv() may return short)."""
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("server closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return chunks[0] if len(chunks) == 1 else b"".join(chunks)


class NetworkEventsIterator:
    """Connects to a running event streamer (see the module docstring for
    which ones) and iterates its CD event stream in whatever batches the
    streamer sends — no local re-slicing to a fixed delta_t, since
    PeriodicFrameGenerationAlgorithm only needs monotonically increasing
    timestamps, not fixed-size chunks.

    Args:
        address: "host:port" of the streamer instance, e.g.
                  "169.254.10.10:9000".
    """

    def __init__(self, address: str) -> None:
        host, _, port_str = address.rpartition(":")
        if not host or not port_str:
            raise ValueError(f"Expected host:port, got {address!r}")
        port = int(port_str)

        self._sock = socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT_S)
        self._sock.settimeout(None)  # blocking reads for the rest of the stream

        try:
            magic, version, width, height = struct.unpack(
                _HEADER_FMT, _recv_exact(self._sock, _HEADER_SIZE)
            )
        except (ConnectionError, struct.error) as exc:
            self._sock.close()
            raise ConnectionError(f"Handshake with {address} failed: {exc}") from exc

        if magic != _MAGIC:
            self._sock.close()
            raise ConnectionError(f"{address} is not a recognized event streamer (bad magic {magic!r})")
        if version not in _PROTOCOL_VERSIONS:
            self._sock.close()
            raise ConnectionError(f"Unsupported protocol version {version} from {address} (expected one of {_PROTOCOL_VERSIONS})")

        self.width = width
        self.height = height
        self._closed = False
        self._last_t: int | None = None  # see __iter__'s cross-batch monotonicity check

        # Remote RAW recording (protocol version 2). start/stop are called
        # from the UI thread, control messages arrive on the reader thread.
        self.supports_remote_raw = version >= _REMOTE_RAW_MIN_VERSION
        self._raw_lock = threading.Lock()
        self._raw_path: str | None = None   # local destination of the current recording
        self._raw_recording = False         # between start and stop requests
        self._raw_pending = False           # stop sent, file not yet fully received
        self._raw_file = None               # open "<path>.part" during the transfer
        self._raw_received = 0

        print(f"Connected to {address}  |  {self.width}×{self.height}")

    def __iter__(self):
        while not self._closed:
            try:
                (count,) = struct.unpack(_COUNT_FMT, _recv_exact(self._sock, _COUNT_SIZE))
            except ConnectionError:
                return
            if count == _CONTROL_MARKER:
                try:
                    self._read_control()
                except ConnectionError:
                    return
                continue
            if count > _MAX_EVENTS_PER_BATCH:
                raise RuntimeError(
                    f"implausible event count ({count}) received in one batch — the TCP "
                    "stream appears desynced; refusing to decode it as events"
                )
            try:
                raw = _recv_exact(self._sock, count * EVENT_CD_DTYPE.itemsize) if count else b""
            except ConnectionError:
                return
            # .copy() rather than handing out the frombuffer() view directly:
            # that view is read-only (backed by an immutable bytes object),
            # and downstream native SDK calls (PeriodicFrameGenerationAlgorithm
            # et al.) assume ordinary writable event arrays like the ones
            # EventsIterator produces locally — pass this straight through
            # without ever confirming a read-only buffer is actually safe there.
            batch = np.frombuffer(raw, dtype=EVENT_CD_DTYPE).copy()
            # Originally fatal on either check below, on the assumption that
            # non-monotonic data could only mean the TCP stream itself was
            # desynced (see _MAX_EVENTS_PER_BATCH above, which still is fatal
            # for that case — a garbage count is never legitimate). Real
            # testing against a GenX320 showed a second, recoverable cause:
            # the sensor's own EVT3 decoder occasionally logging "TimeHigh
            # discrepancy" — a transient hardware/driver hiccup, server-side,
            # that produces one bad-but-correctly-framed batch rather than
            # corrupting the wire protocol. Crashing the whole viewer over
            # one bad batch was too harsh, especially since this recurs —
            # drop just this batch (never hand bad timestamps to the frame
            # generator) and keep going, same as a dropped/coalesced batch
            # anywhere else in this pipeline.
            if batch.size > 1 and (np.diff(batch["t"].astype(np.int64)) < 0).any():
                print("[WARN] dropped a non-monotonic event batch from the network "
                      "(likely a transient sensor/decoder hiccup, not a protocol error)")
                continue
            if batch.size > 0 and self._last_t is not None and int(batch["t"][0]) < self._last_t:
                print("[WARN] dropped an event batch that went backward in time relative to the "
                      "previous one (likely the same transient sensor/decoder hiccup)")
                continue
            if batch.size > 0:
                self._last_t = int(batch["t"][-1])
            yield batch

    def set_recording(self, active: bool) -> None:
        """Tells the server to start/stop treating its backlog as something
        that must survive a reconnect (lossless), for as long as this client
        is actually recording what it receives — see visualizer.py's
        _start_hdf5()/_stop_hdf5(), the only callers. Safe to call
        concurrently with __iter__ consuming the same socket from another
        thread (independent send/recv directions on one TCP connection).
        Best-effort: if the connection is already dead, the iterator side
        will discover that on its own on the next read."""
        try:
            self._sock.sendall(_CMD_START_RECORDING if active else _CMD_STOP_RECORDING)
        except OSError:
            pass

    # ── Remote RAW recording (protocol version 2) ─────────────────────────────

    def start_remote_raw(self, local_path: str) -> bool:
        """Asks the server to start recording RAW to its own storage. The
        file is copied to local_path after stop_remote_raw()."""
        if not self.supports_remote_raw:
            return False
        with self._raw_lock:
            if self._raw_recording:
                return False
            if self._raw_pending:
                print("[INFO] Previous RAW recording is still being copied from the device — try again shortly.")
                return False
            name = os.path.basename(local_path).encode()
            try:
                self._sock.sendall(_CMD_START_RAW + bytes([len(name)]) + name)
            except (OSError, ValueError) as exc:
                print(f"[WARN] could not start RAW recording on the device: {exc}")
                return False
            self._raw_path = local_path
            self._raw_recording = True
        print(f"RAW recording started on the device: {os.path.basename(local_path)}")
        return True

    def stop_remote_raw(self) -> None:
        with self._raw_lock:
            if not self._raw_recording:
                return
            self._raw_recording = False
            try:
                self._sock.sendall(_CMD_STOP_RAW)
            except OSError as exc:
                print(f"[WARN] could not stop RAW recording on the device: {exc}")
                return
            self._raw_pending = True
        print("RAW recording stopped — copying it from the device …")

    def finish_remote_raw(self) -> None:
        """Blocks until a recording that has been stopped is fully received,
        discarding event batches meanwhile. Only for use once nothing else is
        iterating this object any more (i.e. at shutdown)."""
        self.stop_remote_raw()
        try:
            while self._raw_pending and not self._closed:
                (count,) = struct.unpack(_COUNT_FMT, _recv_exact(self._sock, _COUNT_SIZE))
                if count == _CONTROL_MARKER:
                    self._read_control()
                elif count > _MAX_EVENTS_PER_BATCH:
                    raise ConnectionError("stream desynced")
                elif count:
                    _recv_exact(self._sock, count * EVENT_CD_DTYPE.itemsize)
        except (ConnectionError, OSError) as exc:
            print(f"[WARN] connection lost while copying the RAW recording: {exc}")

    def _read_control(self) -> None:
        kind, length = struct.unpack(_CONTROL_FMT, _recv_exact(self._sock, _CONTROL_SIZE))
        payload = _recv_exact(self._sock, length) if length else b""
        with self._raw_lock:
            if kind == _CTRL_RECORDING_ERROR:
                print(f"[WARN] RAW recording on the device failed: {payload.decode(errors='replace')}")
                self._abort_raw_locked()
            elif kind == _CTRL_FILE_BEGIN and self._raw_path is not None:
                (size,) = struct.unpack("<Q", payload)
                self._raw_file = open(self._raw_path + ".part", "wb")
                self._raw_received = 0
                print(f"Receiving {os.path.basename(self._raw_path)} ({size / 1e6:.1f} MB) …")
            elif kind == _CTRL_FILE_DATA and self._raw_file is not None:
                self._raw_file.write(payload)
                self._raw_received += len(payload)
            elif kind == _CTRL_FILE_END and self._raw_file is not None:
                self._raw_file.close()
                self._raw_file = None
                os.replace(self._raw_path + ".part", self._raw_path)
                print(f"RAW recording saved: {self._raw_path} ({self._raw_received / 1e6:.1f} MB)")
                self._raw_path = None
                self._raw_pending = False

    def _abort_raw_locked(self) -> None:
        if self._raw_file is not None:
            self._raw_file.close()
            self._raw_file = None
            print(f"[WARN] incomplete RAW recording left at {self._raw_path}.part")
        self._raw_path = None
        self._raw_recording = False
        self._raw_pending = False

    def close(self) -> None:
        self._closed = True
        with self._raw_lock:
            if self._raw_recording or self._raw_pending:
                print("[WARN] RAW recording was not fully copied — the original is still on the device.")
            self._abort_raw_locked()
        try:
            self._sock.close()
        except OSError:
            pass
