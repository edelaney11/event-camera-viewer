"""Iterate through a list of HDF5 recording files as a single playlist.

next_file() / prev_file() may be called from any thread; the jump takes
effect between slices in the background event thread.
"""
from __future__ import annotations

import threading
from pathlib import Path

from hdf5_reader import HDF5EventsIterator
from raw_reader import RawEventsIterator


def _open_file(path: str, delta_t_us: int, replay_speed: float):
    if path.lower().endswith(".raw"):
        return RawEventsIterator(path, delta_t_us, replay_speed)
    return HDF5EventsIterator(path, delta_t_us, replay_speed)


class PlaylistIterator:
    def __init__(
        self,
        paths: list[str],
        delta_t_us: int = 10_000,
        replay_speed: float = 1.0,
    ) -> None:
        if not paths:
            raise ValueError("Playlist is empty")
        self._paths = paths
        self._delta_t_us = delta_t_us
        self._replay_speed = replay_speed

        self._index: int = 0
        self._lock = threading.Lock()
        self._jump_to: int | None = None
        self._current_it = None

        # Probe the first file for sensor dimensions
        probe = _open_file(paths[0], delta_t_us, replay_speed)
        self.width  = probe.width
        self.height = probe.height
        probe.close()

    @property
    def current_index(self) -> int:
        return self._index

    @property
    def total(self) -> int:
        return len(self._paths)

    @property
    def current_name(self) -> str:
        return Path(self._paths[self._index]).name

    def next_file(self) -> None:
        with self._lock:
            if self._index < len(self._paths) - 1:
                self._jump_to = self._index + 1

    def prev_file(self) -> None:
        with self._lock:
            if self._index > 0:
                self._jump_to = self._index - 1

    @property
    def replay_speed(self) -> float:
        return self._replay_speed

    @replay_speed.setter
    def replay_speed(self, value: float) -> None:
        self._replay_speed = max(value, 0.0)
        if self._current_it is not None:
            self._current_it.replay_speed = self._replay_speed

    def __iter__(self):
        while self._index < len(self._paths):
            it = _open_file(
                self._paths[self._index],
                self._delta_t_us,
                self._replay_speed,
            )
            self._current_it = it
            jumped = False
            for evs in it:
                with self._lock:
                    target = self._jump_to
                    if target is not None:
                        self._jump_to = None
                        self._index = target
                        jumped = True
                if jumped:
                    break
                yield evs
            it.close()
            self._current_it = None
            if not jumped:
                self._index += 1

    def close(self) -> None:
        pass
