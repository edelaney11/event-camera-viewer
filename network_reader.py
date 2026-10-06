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
        u32  version   protocol version (currently 1)
        u16  width
        u16  height

    Then, repeated for the life of the connection:
        u32  count               number of events in this batch
        count * 16-byte events   see EVENT_CD_DTYPE (hdf5_reader.py) —
                                  x:u2, y:u2, p:i2, t:i8, with 2 bytes of
                                  padding between p and t

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

import socket
import struct

import numpy as np

from hdf5_reader import EVENT_CD_DTYPE

_HEADER_FMT = "<4sIHH"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)
_MAGIC = b"PECD"
_PROTOCOL_VERSION = 1
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
# connection). Understood by genx320_streamer.py/genx320_streamer_native: a
# server starts in a discard-backlog-on-reconnect "live view" mode, and
# these switch it to/from a lossless mode that preserves backlog across a
# reconnect instead, for the duration of an actual recording.
# tcp_event_streamer.cpp doesn't read these (it's unconditionally lossless
# once a client connects) — harmless, unread bytes left in its receive buffer.
_CMD_STOP_RECORDING = b"\x00"
_CMD_START_RECORDING = b"\x01"


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
        if version != _PROTOCOL_VERSION:
            self._sock.close()
            raise ConnectionError(f"Unsupported protocol version {version} from {address} (expected {_PROTOCOL_VERSION})")

        self.width = width
        self.height = height
        self._closed = False
        self._last_t: int | None = None  # see __iter__'s cross-batch monotonicity check

        print(f"Connected to {address}  |  {self.width}×{self.height}")

    def __iter__(self):
        while not self._closed:
            try:
                (count,) = struct.unpack(_COUNT_FMT, _recv_exact(self._sock, _COUNT_SIZE))
            except ConnectionError:
                return
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

    def close(self) -> None:
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass
