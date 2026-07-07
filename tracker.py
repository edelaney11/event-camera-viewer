"""Frame-based object tracker wrapping OpenCV's tracking algorithms."""
from __future__ import annotations

from collections import deque

import cv2
import numpy as np

ALGORITHMS: dict[str, callable] = {
    name.replace("Tracker", "").replace("_create", ""): getattr(cv2, name)
    for name in dir(cv2)
    if name.startswith("Tracker") and name.endswith("_create")
}

_TRAIL_LEN = 60


class ObjectTracker:
    def __init__(self, algorithm: str = "MIL") -> None:
        key = algorithm.upper()
        if key not in ALGORITHMS:
            raise ValueError(f"Unknown algorithm '{algorithm}'. Choose from: {list(ALGORITHMS)}")
        self._factory = ALGORITHMS[key]
        self._tracker = None
        self._bbox: tuple[int, int, int, int] | None = None  # x, y, w, h
        self._active: bool = False
        self._lost: bool = False
        self._trail: deque[tuple[int, int]] = deque(maxlen=_TRAIL_LEN)

    def initialize(self, frame: np.ndarray, bbox: tuple) -> None:
        self._tracker = self._factory()
        self._bbox = tuple(int(v) for v in bbox)
        self._tracker.init(frame, self._bbox)
        self._active = True
        self._lost = False
        self._trail.clear()
        self._trail.append(self._center())

    def update(self, frame: np.ndarray) -> tuple[bool, tuple[int, int, int, int] | None]:
        if not self._active or self._tracker is None:
            return False, None
        ok, raw = self._tracker.update(frame)
        if ok:
            self._bbox = tuple(int(v) for v in raw)
            self._lost = False
            self._trail.append(self._center())
        else:
            self._lost = True
        return ok, self._bbox

    def reset(self) -> None:
        self._tracker = None
        self._bbox = None
        self._active = False
        self._lost = False
        self._trail.clear()

    def _center(self) -> tuple[int, int]:
        x, y, w, h = self._bbox
        return (x + w // 2, y + h // 2)

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def is_lost(self) -> bool:
        return self._lost

    @property
    def bbox(self) -> tuple[int, int, int, int] | None:
        return self._bbox

    @property
    def trail(self) -> list[tuple[int, int]]:
        return list(self._trail)
