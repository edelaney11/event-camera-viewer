#!/usr/bin/env python3
"""Export an HDF5 or RAW event camera file to an MP4 video.

Uses the same Dark-palette time-surface rendering as the interactive viewer.

Usage:
    python export_mp4.py --input recording.hdf5
    python export_mp4.py --input recording.raw --output clip.mp4 --fps 60 --accum-us 10000
"""
from __future__ import annotations

import os
import sys

# ── OpenEB environment bootstrap ──────────────────────────────────────────────
_INSTALL    = os.environ.get("OPENEB_INSTALL_DIR", os.path.expanduser("~/openeb/install"))
_LIBDIR     = os.path.join(_INSTALL, "lib")
_PYVER      = f"python{sys.version_info.major}.{sys.version_info.minor}"
_PYDIR      = os.path.join(_INSTALL, "lib", _PYVER, "dist-packages")
_HAL_PLUGINS  = os.path.join(_INSTALL, "lib", "metavision", "hal", "plugins")
_HDF5_PLUGINS = os.path.join(_INSTALL, "lib", "hdf5", "plugin")

_need_reexec = False
for _envvar, _path in [
    ("LD_LIBRARY_PATH",    _LIBDIR),
    ("MV_HAL_PLUGIN_PATH", _HAL_PLUGINS),
    ("HDF5_PLUGIN_PATH",   _HDF5_PLUGINS),
]:
    _curr = os.environ.get(_envvar, "")
    if _path not in _curr:
        os.environ[_envvar] = f"{_path}:{_curr}" if _curr else _path
        _need_reexec = True

if _PYDIR not in sys.path:
    sys.path.insert(0, _PYDIR)

if _need_reexec:
    os.execv(sys.executable, [sys.executable] + sys.argv)

# ── Normal imports ─────────────────────────────────────────────────────────────

import argparse

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ── Rendering ─────────────────────────────────────────────────────────────────

# Dark palette colours in BGR (from ColorPalette.Dark SDK values)
_BG  = np.array([52.,  37.,  30.],  dtype=np.float32)
_ON  = np.array([255., 255., 255.], dtype=np.float32)
_OFF = np.array([200., 126., 64.],  dtype=np.float32)


def render_frame(ts_surface: np.ndarray, pol_surface: np.ndarray,
                 frame_ts: int, accum_us: int) -> np.ndarray:
    """Render a single video frame from the current time-surface state."""
    H, W = ts_surface.shape
    active = ts_surface >= 0

    if not np.any(active):
        return np.broadcast_to(_BG.astype(np.uint8), (H, W, 3)).copy()

    age = np.where(active, frame_ts - ts_surface, accum_us)
    alpha = np.clip(
        1.0 - age.astype(np.float32) / max(accum_us, 1),
        0.0, 1.0,
    )[:, :, np.newaxis]

    on_mask = (pol_surface > 0)[:, :, np.newaxis]
    target  = np.where(on_mask, _ON, _OFF)

    frame = (_BG + alpha * (target - _BG)).clip(0, 255).astype(np.uint8)
    frame[~active] = _BG.astype(np.uint8)
    return frame


# ── Export ────────────────────────────────────────────────────────────────────

def export(input_path: str, output_path: str, fps: int, accum_us: int) -> int:
    frame_interval_us = 1_000_000 // fps

    if input_path.lower().endswith(".raw"):
        from raw_reader import RawEventsIterator
        it = RawEventsIterator(input_path, delta_t_us=frame_interval_us, replay_speed=0)
    else:
        from hdf5_reader import HDF5EventsIterator
        it = HDF5EventsIterator(input_path, delta_t_us=frame_interval_us, replay_speed=0)

    W, H = it.width, it.height

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(output_path, fourcc, fps, (W, H))
    if not writer.isOpened():
        print(f"Error: could not open VideoWriter for {output_path}", file=sys.stderr)
        it.close()
        return 1

    ts_surface  = np.full((H, W), -1, dtype=np.int64)
    pol_surface = np.zeros((H, W), dtype=np.int8)

    frame_count  = 0
    t_start      = None
    next_frame_ts = 0

    print(f"Input:    {input_path}")
    print(f"Output:   {output_path}")
    print(f"FPS:      {fps}  |  accum: {accum_us // 1000} ms  |  sensor: {W}×{H}")
    print("Exporting ...")

    try:
        for evs in it:
            if len(evs) == 0:
                continue

            if t_start is None:
                t_start = int(evs["t"][0])
                next_frame_ts = t_start + frame_interval_us

            ts_surface[evs["y"], evs["x"]] = evs["t"]
            pol_surface[evs["y"], evs["x"]] = evs["p"]
            slice_end_ts = int(evs["t"][-1])

            while slice_end_ts >= next_frame_ts:
                writer.write(render_frame(ts_surface, pol_surface, next_frame_ts, accum_us))
                frame_count  += 1
                next_frame_ts += frame_interval_us

                if frame_count % (fps * 5) == 0:
                    elapsed_s = (next_frame_ts - t_start) / 1_000_000
                    print(f"  {elapsed_s:.0f}s processed ({frame_count} frames) ...", end="\r")

    finally:
        writer.release()
        it.close()

    duration_s = frame_count / fps
    print(f"\nDone: {frame_count} frames ({duration_s:.1f} s) → {output_path}")
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Export an HDF5 or RAW event camera file to an MP4 video."
    )
    p.add_argument("--input", required=True, metavar="FILE",
                   help="HDF5 or RAW file to export.")
    p.add_argument("--output", metavar="FILE",
                   help="Output MP4 path (default: same name with .mp4).")
    p.add_argument("--fps", type=int, default=30,
                   help="Output video frame rate (default: 30).")
    p.add_argument("--accum-us", type=int, default=20_000, metavar="US",
                   help="Event decay window in µs — controls how long events "
                        "stay visible (default: 20000).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output or (os.path.splitext(args.input)[0] + ".mp4")
    return export(args.input, output, args.fps, args.accum_us)


if __name__ == "__main__":
    raise SystemExit(main())
