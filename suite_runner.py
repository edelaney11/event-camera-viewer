"""Automated bias-sweep suite runner.

Loads a JSON config, applies each setting in turn, records for the
specified duration, then advances to the next automatically.

Duration precision
------------------
Recording boundaries are determined by the camera's own event timestamps,
not wall-clock time.  receive_events() (background thread) clips each HDF5
batch at exactly  rec_start_ts + duration_us  and sets _cutoff_reached.
tick() (main thread) acts on that flag, so display-frame-rate jitter has
no effect on how many events end up in each file.

Thread model
------------
receive_events()  — background event thread; uses _hdf5_lock for the
                    pending buffer, GIL-safe atomics for timestamp flags.
tick() / flush_hdf5() — must be called from the main thread.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

from camera_manager import CameraManager
from hdf5_writer import HDF5Writer


@dataclass
class SuiteSetting:
    name: str
    biases: dict[str, int]
    duration_s: float


class SuiteRunner:
    """Run through a list of bias configurations, recording each one."""

    _IDLE      = "idle"
    _RECORDING = "recording"   # covers both settle and record phases
    _DONE      = "done"

    def __init__(self, config_path: str, camera: CameraManager) -> None:
        self._camera = camera
        self._config_path = Path(config_path)
        self._settings: list[SuiteSetting] = []
        self._global_duration_s: float = 5.0
        self._settle_s: float = 0.5
        self._fmt: str = "hdf5"
        self._output_dir: Path = Path("suite_output")

        self._load(config_path)

        self._state: str = self._IDLE
        self._index: int = -1
        self._step_wall_start: float = 0.0   # wall clock when step began (for settle)
        self._suite_start_time: str = ""
        self._meta_steps: list[dict] = []
        self._actual_start: float = 0.0

        # Timestamp-based cutoff (set/read across threads — GIL-safe int/bool)
        self._rec_start_ts_us: int = -1      # first post-settle event ts
        self._rec_end_ts_us: int = -1        # rec_start + duration_us
        self._latest_ts_us: int = -1         # most recent event ts seen
        self._cutoff_reached: bool = False   # set by receive_events when done

        # HDF5 recording
        self._hdf5_lock = threading.Lock()
        self._hdf5_writer: Optional[HDF5Writer] = None
        self._hdf5_pending: list[np.ndarray] = []

        self._step_event_count: int = 0

        # Deferred RAW recording (started after settle, not before)
        self._raw_recording_pending: bool = False
        self._pending_raw_path: str = ""

    # ── Config loading ────────────────────────────────────────────────────────

    def _load(self, path: str) -> None:
        with open(path) as f:
            cfg = json.load(f)

        self._global_duration_s = float(cfg.get("duration_s", 5.0))
        self._settle_s = float(cfg.get("settle_s", 0.5))
        self._fmt = cfg.get("format", "hdf5").lower()
        self._output_dir = Path(cfg.get("output_dir", "suite_output"))

        for item in cfg.get("settings", []):
            idx = len(self._settings)
            name = item.get("name", f"setting_{idx + 1:03d}")
            biases = {k: int(v) for k, v in item.get("biases", {}).items()}
            dur = float(item.get("duration_s", self._global_duration_s))
            self._settings.append(SuiteSetting(name, biases, dur))

        if not self._settings:
            raise ValueError("Suite config contains no settings")

        print(
            f"Suite loaded: {len(self._settings)} setting(s), "
            f"default {self._global_duration_s}s each, "
            f"settle {self._settle_s}s, format={self._fmt}"
        )

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        return self._state not in (self._IDLE, self._DONE)

    @property
    def is_done(self) -> bool:
        return self._state == self._DONE

    @property
    def is_settling(self) -> bool:
        if self._state != self._RECORDING:
            return False
        return (time.monotonic() - self._step_wall_start) < self._settle_s

    @property
    def is_recording(self) -> bool:
        """True once settle is over and we are actively capturing."""
        if self._state != self._RECORDING:
            return False
        return (time.monotonic() - self._step_wall_start) >= self._settle_s

    @property
    def index(self) -> int:
        return self._index

    @property
    def total(self) -> int:
        return len(self._settings)

    @property
    def current_setting(self) -> Optional[SuiteSetting]:
        if 0 <= self._index < len(self._settings):
            return self._settings[self._index]
        return None

    @property
    def elapsed_record_s(self) -> float:
        """Elapsed recording time in seconds (by event timestamp when available)."""
        if self._state != self._RECORDING or self.is_settling:
            return 0.0
        if self._rec_start_ts_us >= 0 and self._latest_ts_us >= self._rec_start_ts_us:
            return (self._latest_ts_us - self._rec_start_ts_us) / 1_000_000
        # No events yet — fall back to wall clock
        return max(0.0, time.monotonic() - self._step_wall_start - self._settle_s)

    @property
    def duration_s(self) -> float:
        s = self.current_setting
        return s.duration_s if s else self._global_duration_s

    @property
    def settle_s(self) -> float:
        return self._settle_s

    # ── Control interface ─────────────────────────────────────────────────────

    def start_or_stop(self) -> None:
        if self._state in (self._IDLE, self._DONE):
            self._index = -1
            self._meta_steps.clear()
            self._actual_start = time.monotonic()
            self._suite_start_time = datetime.now().isoformat(timespec="seconds")
            print(f"\n{'='*50}")
            print(f"Suite starting: {len(self._settings)} setting(s)")
            print(f"{'='*50}")
            self._advance()
        else:
            self._stop_step()
            self._state = self._IDLE
            print("Suite aborted.")

    def skip(self) -> None:
        if self.is_active:
            print(f"  Skipping '{self.current_setting.name}' at {self.elapsed_record_s:.3f}s")
            self._stop_step()
            self._advance()

    def tick(self) -> None:
        """Call from the main thread each frame.

        Advances to the next step when the background thread signals that
        the timestamp-based cutoff has been reached.  Falls back to a wall-
        clock timeout if the scene produces no events.
        """
        if self._state != self._RECORDING:
            return

        if self._raw_recording_pending and not self.is_settling:
            self._camera.start_raw_recording(self._pending_raw_path)
            self._raw_recording_pending = False

        if self._cutoff_reached:
            self._stop_step()
            self._advance()
            return

        # Secondary timestamp check: at very high event rates the background
        # thread can lag real-time (large batch copies), so _cutoff_reached may
        # not be set before the wall-clock guard below fires.  Checking
        # _latest_ts_us directly handles that case.
        if self._rec_end_ts_us >= 0 and self._latest_ts_us >= self._rec_end_ts_us:
            self._stop_step()
            self._advance()
            return

        # Wall-clock safety net: use a tight timeout only when no post-settle
        # events have arrived (dark/quiet scene).  Once events are flowing,
        # allow much more time so a high-rate scene doesn't trip the guard
        # while the background thread catches up.
        wall_elapsed = time.monotonic() - self._step_wall_start
        if self._rec_start_ts_us < 0:
            timeout = self._settle_s + self.duration_s * 2
        else:
            timeout = self._settle_s + self.duration_s * 10
        if wall_elapsed > timeout:
            print(f"  [WARN] No events reached cutoff — advancing after {wall_elapsed:.1f}s wall clock")
            self._stop_step()
            self._advance()

    def receive_events(self, events: np.ndarray) -> None:
        """Feed events from the background thread.

        Called with every event slice.  Clips each batch precisely at
        rec_end_ts_us so HDF5 recordings contain exactly duration_s worth
        of events regardless of display frame rate.
        """
        if self._state != self._RECORDING or self._cutoff_reached:
            return

        # Settle phase: track timestamps but don't record
        wall_elapsed = time.monotonic() - self._step_wall_start
        if wall_elapsed < self._settle_s:
            return

        if len(events) == 0:
            return

        # Update latest timestamp (read by elapsed_record_s on main thread)
        self._latest_ts_us = int(events["t"][-1])

        # Latch the recording start timestamp on the first post-settle batch
        if self._rec_start_ts_us < 0:
            self._rec_start_ts_us = int(events["t"][0])
            self._rec_end_ts_us   = self._rec_start_ts_us + int(self.duration_s * 1_000_000)

        # Clip the batch to the cutoff timestamp
        end_ts = self._rec_end_ts_us
        if events["t"][-1] >= end_ts:
            events = events[events["t"] < end_ts]
            self._cutoff_reached = True   # signal tick() to advance

        self._step_event_count += len(events)

        with self._hdf5_lock:
            if self._hdf5_writer is not None and len(events) > 0:
                self._hdf5_pending.append(events.copy())

    def flush_hdf5(self) -> None:
        """Write buffered events to disk. Must be called from the main thread."""
        with self._hdf5_lock:
            if not self._hdf5_pending:
                return
            pending, self._hdf5_pending = self._hdf5_pending, []
            writer = self._hdf5_writer
        if writer is not None:
            for batch in pending:
                writer.write(batch)

    # ── Internal state machine ────────────────────────────────────────────────

    def _advance(self) -> None:
        self._index += 1
        if self._index >= len(self._settings):
            self._state = self._DONE
            self._write_metadata()
            elapsed = time.monotonic() - self._actual_start
            print(f"\n{'='*50}")
            print(f"Suite complete! {self.total} setting(s) in {elapsed:.1f}s")
            print(f"Results: {self._output_dir}/")
            print(f"{'='*50}\n")
            return

        setting = self._settings[self._index]
        print(f"\n[{self._index + 1}/{self.total}] {setting.name}")

        for name, val in setting.biases.items():
            ok = self._camera.set_bias(name, val)
            flag = "" if ok else "  ← WARN: failed"
            print(f"  {name} = {val}{flag}")

        # Set wall-start BEFORE clearing cutoff_reached so the background
        # thread's settle guard sees wall_elapsed≈0 if it races here.
        self._step_wall_start = time.monotonic()

        # Reset timestamp trackers for the new step
        self._rec_start_ts_us = -1
        self._rec_end_ts_us   = -1
        self._latest_ts_us    = -1
        self._cutoff_reached  = False
        self._step_event_count = 0

        # Start recordings
        self._output_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{self._index + 1:03d}_{setting.name}"
        output_files: list[str] = []

        if self._fmt in ("raw", "both"):
            raw_path = str(self._output_dir / f"{stem}.raw")
            self._pending_raw_path = raw_path
            self._raw_recording_pending = True
            output_files.append(f"{stem}.raw")

        if self._fmt in ("hdf5", "both"):
            hdf5_path = str(self._output_dir / f"{stem}.hdf5")
            writer = HDF5Writer(hdf5_path, self._camera.width, self._camera.height)
            with self._hdf5_lock:
                self._hdf5_writer = writer
                self._hdf5_pending.clear()
            output_files.append(f"{stem}.hdf5")

        self._state = self._RECORDING

        print(
            f"  settle {self._settle_s}s → record {setting.duration_s}s  "
            f"→ {', '.join(output_files)}"
        )

        self._meta_steps.append({
            "index":            self._index + 1,
            "name":             setting.name,
            "biases":           dict(setting.biases),
            "target_duration_s": setting.duration_s,
            "actual_duration_s": None,
            "output_files":     output_files,
        })

    def _stop_step(self) -> None:
        actual = self.elapsed_record_s
        if self._meta_steps:
            self._meta_steps[-1]["actual_duration_s"] = round(actual, 6)
            self._meta_steps[-1]["total_events"] = self._step_event_count

        self._raw_recording_pending = False
        self._pending_raw_path = ""
        self._camera.stop_raw_recording()

        with self._hdf5_lock:
            writer = self._hdf5_writer
            self._hdf5_writer = None
            pending, self._hdf5_pending = self._hdf5_pending, []
        if writer is not None:
            for batch in pending:
                writer.write(batch)
            writer.close()

    def _write_metadata(self) -> None:
        total_events = sum(s.get("total_events", 0) for s in self._meta_steps)
        meta = {
            "suite_config":   str(self._config_path),
            "start_time":     self._suite_start_time,
            "total_settings": self.total,
            "total_events":   total_events,
            "format":         self._fmt,
            "settle_s":       self._settle_s,
            "steps":          self._meta_steps,
        }
        path = self._output_dir / "suite_metadata.json"
        with open(path, "w") as f:
            json.dump(meta, f, indent=2)
        print(f"Metadata saved: {path}")
