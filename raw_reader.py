"""Read events from a Prophesee .raw file and replay them in delta_t slices.

Built on metavision_core's RawReader (rather than the device/EventsIterator
path used for live cameras) because it's the SDK class meant for offline,
seekable file reading — seek_time()/load_delta_t() let us support scrubbing
via seek(), and it shares the same file-relative (time-shifted) timestamp
domain as get_raw_info(), which the RAW-splitting feature also relies on.

seek_time() only moves forward (it works by reading and discarding events up
to the target, internally); seeking to a point earlier than the reader's
current position needs a reset() first — see __iter__.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from metavision_core.event_io import RawReader


class RawEventsIterator:
    """Iterate through a .raw recording in fixed time slices.

    Args:
        path:              Path to the .raw file.
        delta_t_us:        Duration of each yielded slice in microseconds.
        replay_speed:      1.0 = real time; 2.0 = double speed; 0 = as fast as possible.
        keep_alive_at_eof: If True, idle (rather than end iteration) once the file is
                            fully read, so a later seek() can resume playback — used for
                            single-file playback with a scrub bar. Playlists leave this
                            False so reaching EOF still advances to the next file.
    """

    def __init__(
        self,
        path: str,
        delta_t_us: int = 10_000,
        replay_speed: float = 1.0,
        keep_alive_at_eof: bool = False,
    ) -> None:
        self.path = path
        self._delta_t_us = delta_t_us
        self._replay_speed = max(replay_speed, 0.0)
        self._keep_alive_at_eof = keep_alive_at_eof

        self._reader = RawReader(path)
        height, width = self._reader.get_size()
        self.width  = width
        self.height = height

        self._lock = threading.Lock()
        self._seek_to_us: Optional[int] = None
        self._duration_us: Optional[int] = None  # lazily computed — see duration_us

        print(
            f"Loaded {path}  |  "
            f"{self.width}×{self.height}  |  "
            f"speed ×{self._replay_speed if self._replay_speed else '∞'}"
        )

    def __iter__(self):
        while True:
            with self._lock:
                target_us, self._seek_to_us = self._seek_to_us, None
            if target_us is not None:
                if target_us <= 0:
                    self._reader.reset()
                else:
                    try:
                        self._reader.seek_time(target_us)
                    except RuntimeError:
                        # seek_time() can only advance forward (it works by reading
                        # and discarding events up to the target) — for a target
                        # earlier than the reader's current position, rewind first.
                        self._reader.reset()
                        self._reader.seek_time(target_us)

            if self._reader.is_done():
                if not self._keep_alive_at_eof:
                    return
                time.sleep(0.05)  # idle, waiting for a possible seek() back into the file
                continue

            evs = self._reader.load_delta_t(self._delta_t_us)
            yield evs
            if self._replay_speed > 0:
                time.sleep(self._delta_t_us / (1e6 * self._replay_speed))

    def seek(self, timestamp_us: int) -> None:
        """Request a jump to a file-relative timestamp (µs). Thread-safe — the
        actual seek happens on the background event thread, on its next slice."""
        with self._lock:
            self._seek_to_us = max(0, int(timestamp_us))

    @property
    def duration_us(self) -> int:
        """Total file duration in µs. Computed (and cached to a sidecar JSON) on
        first access via get_raw_info, since scanning the whole file is only
        worth the cost when something (e.g. a seek bar) actually needs it."""
        if self._duration_us is None:
            from metavision_core.event_io import get_raw_info
            self._duration_us = int(get_raw_info(self.path)["duration"])
        return self._duration_us

    @property
    def replay_speed(self) -> float:
        return self._replay_speed

    @replay_speed.setter
    def replay_speed(self, value: float) -> None:
        self._replay_speed = max(value, 0.0)

    def close(self) -> None:
        pass
