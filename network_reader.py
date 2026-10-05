"""Read CD events live from a Prophesee Onboard camera over TCP.

Connects to a tcp_event_streamer instance (see onboard_streamer/) running on
the camera's embedded host and yields event batches as numpy structured
arrays, in the same (x, y, p, t) shape _event_loop (visualizer.py) already
consumes from RawEventsIterator / HDF5EventsIterator / the SDK's own
EventsIterator.

Wire protocol (all integers little-endian — both the Onboard's ARM Linux and
this viewer's x86 Linux host are little-endian, so no byte-swapping is
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


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Block until exactly n bytes have been read (recv() may return short)."""
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("tcp_event_streamer closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return chunks[0] if len(chunks) == 1 else b"".join(chunks)


class NetworkEventsIterator:
    """Connects to a running tcp_event_streamer and iterates its CD event
    stream in whatever batches the streamer sends — no local re-slicing to
    a fixed delta_t, since PeriodicFrameGenerationAlgorithm only needs
    monotonically increasing timestamps, not fixed-size chunks.

    Args:
        address: "host:port" of the tcp_event_streamer instance, e.g.
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
            raise ConnectionError(f"{address} is not a tcp_event_streamer (bad magic {magic!r})")
        if version != _PROTOCOL_VERSION:
            self._sock.close()
            raise ConnectionError(f"Unsupported protocol version {version} from {address} (expected {_PROTOCOL_VERSION})")

        self.width = width
        self.height = height
        self._closed = False

        print(f"Connected to {address}  |  {self.width}×{self.height}")

    def __iter__(self):
        while not self._closed:
            try:
                (count,) = struct.unpack(_COUNT_FMT, _recv_exact(self._sock, _COUNT_SIZE))
                raw = _recv_exact(self._sock, count * EVENT_CD_DTYPE.itemsize) if count else b""
            except ConnectionError:
                return
            yield np.frombuffer(raw, dtype=EVENT_CD_DTYPE)

    def close(self) -> None:
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass
