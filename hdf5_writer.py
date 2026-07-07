"""HDF5 event recorder using resizable datasets.

Layout:
  /events/x   uint16
  /events/y   uint16
  /events/p   int16
  /events/t   int64 (microseconds)
  attrs: width, height, event_count

Events are buffered in memory and written to disk in large chunks to
amortise the cost of HDF5 dataset resize operations.
"""
from __future__ import annotations

import numpy as np
import h5py

# Write to disk once this many events have been buffered.
# At ~1–5 Mev/s this means a flush every 0.1–0.5 s — far fewer
# resize calls than flushing on every 10 ms iterator slice.
_FLUSH_EVENTS = 500_000
_CHUNK = _FLUSH_EVENTS


class HDF5Writer:
    def __init__(self, path: str, width: int, height: int) -> None:
        self.path = path
        self._file = h5py.File(path, "w", libver="latest")
        self._file.attrs["width"] = width
        self._file.attrs["height"] = height

        grp = self._file.create_group("events")
        grp.create_dataset("x", shape=(0,), maxshape=(None,), dtype="uint16", chunks=(_CHUNK,))
        grp.create_dataset("y", shape=(0,), maxshape=(None,), dtype="uint16", chunks=(_CHUNK,))
        grp.create_dataset("p", shape=(0,), maxshape=(None,), dtype="int16",  chunks=(_CHUNK,))
        grp.create_dataset("t", shape=(0,), maxshape=(None,), dtype="int64",  chunks=(_CHUNK,))

        self._count: int = 0
        self._closed: bool = False

        # In-memory buffer: list of event arrays waiting to be flushed
        self._buf: list[np.ndarray] = []
        self._buf_count: int = 0

    def write(self, events: np.ndarray) -> None:
        """Accept a batch of events. Flushes to disk when the buffer is full."""
        if self._closed or len(events) == 0:
            return
        self._buf.append(events)
        self._buf_count += len(events)
        if self._buf_count >= _FLUSH_EVENTS:
            self._flush()

    def _flush(self) -> None:
        if not self._buf:
            return
        combined = np.concatenate(self._buf) if len(self._buf) > 1 else self._buf[0]
        n = len(combined)
        new_count = self._count + n
        grp = self._file["events"]
        for field in ("x", "y", "p", "t"):
            ds = grp[field]
            ds.resize((new_count,))
            ds[self._count:new_count] = combined[field]
        self._count = new_count
        self._buf.clear()
        self._buf_count = 0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._flush()
        self._file.attrs["event_count"] = self._count
        self._file.close()
        print(f"HDF5 recording saved: {self.path} ({self._count:,} events)")
