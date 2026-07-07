"""Read events from a Prophesee .raw file and replay them in delta_t slices."""
from __future__ import annotations

import time

from metavision_core.event_io.raw_reader import initiate_device
from metavision_core.event_io import EventsIterator


class RawEventsIterator:
    """Iterate through a .raw recording in fixed time slices.

    Args:
        path:         Path to the .raw file.
        delta_t_us:   Duration of each yielded slice in microseconds.
        replay_speed: 1.0 = real time; 2.0 = double speed; 0 = as fast as possible.
    """

    def __init__(self, path: str, delta_t_us: int = 10_000, replay_speed: float = 1.0) -> None:
        self._delta_t_us = delta_t_us
        self._replay_speed = max(replay_speed, 0.0)

        self._device = initiate_device(path)
        geo = self._device.get_i_geometry()
        self.width  = geo.get_width()
        self.height = geo.get_height()
        self._it = EventsIterator.from_device(device=self._device, delta_t=delta_t_us)

        print(
            f"Loaded {path}  |  "
            f"{self.width}×{self.height}  |  "
            f"speed ×{self._replay_speed if self._replay_speed else '∞'}"
        )

    def __iter__(self):
        for evs in self._it:
            yield evs
            if self._replay_speed > 0:
                time.sleep(self._delta_t_us / (1e6 * self._replay_speed))

    @property
    def replay_speed(self) -> float:
        return self._replay_speed

    @replay_speed.setter
    def replay_speed(self, value: float) -> None:
        self._replay_speed = max(value, 0.0)

    def close(self) -> None:
        pass
