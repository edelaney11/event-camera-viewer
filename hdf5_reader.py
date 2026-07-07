"""Read events from our HDF5 format and replay them in delta_t slices.

Loads the full timestamp array once for O(log n) binary-search slicing.
x/y/p arrays are read on-demand per slice to keep memory usage low for
large files.
"""
from __future__ import annotations

import time

import h5py
import numpy as np

# Must match the EventCD dtype that PeriodicFrameGenerationAlgorithm expects.
# Offsets include 2-byte padding between p and t (verified from metavision_sdk_base).
EVENT_CD_DTYPE = np.dtype({
    "names":   ["x",    "y",    "p",    "t"],
    "formats": ["<u2",  "<u2",  "<i2",  "<i8"],
    "offsets": [0,      2,      4,      8],
    "itemsize": 16,
})


class HDF5EventsIterator:
    """Iterate through a recorded HDF5 event file in fixed time slices.

    Args:
        path:          Path to the HDF5 file written by HDF5Writer.
        delta_t_us:    Duration of each yielded slice in microseconds.
        replay_speed:  1.0 = real time; 2.0 = double speed; 0 = as fast as possible.
    """

    def __init__(self, path: str, delta_t_us: int = 10_000, replay_speed: float = 1.0) -> None:
        self._path = path
        self._delta_t_us = delta_t_us
        self._replay_speed = max(replay_speed, 0.0)

        self._file = h5py.File(path, "r")
        evg = self._file["events"]

        self.width  = int(self._file.attrs.get("width",  0))
        self.height = int(self._file.attrs.get("height", 0))

        # Load full timestamp array upfront for binary-search slicing.
        # At 8 bytes per event, 10M events ≈ 80 MB — acceptable for typical recordings.
        self._t: np.ndarray = evg["t"][:]
        self._x_ds = evg["x"]
        self._y_ds = evg["y"]
        self._p_ds = evg["p"]
        self._n = len(self._t)

        duration_s = (int(self._t[-1]) - int(self._t[0])) / 1e6 if self._n > 1 else 0.0
        print(
            f"Loaded {self._n:,} events  |  "
            f"{self.width}×{self.height}  |  "
            f"duration {duration_s:.1f} s  |  "
            f"speed ×{self._replay_speed if self._replay_speed else '∞'}"
        )

    def get_size(self) -> tuple[int, int]:
        """Return (height, width) to match the EventsIterator interface."""
        return (self.height, self.width)

    @property
    def replay_speed(self) -> float:
        return self._replay_speed

    @replay_speed.setter
    def replay_speed(self, value: float) -> None:
        self._replay_speed = max(value, 0.0)

    def __iter__(self):
        if self._n == 0:
            return

        t_start = int(self._t[0])
        t_end   = int(self._t[-1])
        t_cur   = t_start

        while t_cur <= t_end:
            t_next = t_cur + self._delta_t_us

            # Binary search for the slice boundaries
            i0 = int(np.searchsorted(self._t, t_cur,  side="left"))
            i1 = int(np.searchsorted(self._t, t_next, side="left"))

            n = i1 - i0
            batch = np.empty(n, dtype=EVENT_CD_DTYPE)
            if n > 0:
                batch["x"] = self._x_ds[i0:i1]
                batch["y"] = self._y_ds[i0:i1]
                batch["p"] = self._p_ds[i0:i1]
                batch["t"] = self._t[i0:i1]

            yield batch

            # Real-time pacing: sleep for the wall-clock equivalent of delta_t
            if self._replay_speed > 0:
                time.sleep(self._delta_t_us / (1e6 * self._replay_speed))

            t_cur = t_next

    def close(self) -> None:
        self._file.close()
