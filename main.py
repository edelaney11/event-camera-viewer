#!/usr/bin/env python3
"""Prophesee event camera viewer — bias, ROI, and recording controls.

Live camera:
    python main.py [--serial <SN>] [--slice-us 10000] [--fps 30] [--accum-us 20000]

File playback:
    python main.py --input recording_20240101_120000.hdf5 [--speed 1.0]

Locates a Prophesee event-camera SDK automatically — an OpenEB install is
preferred, with the official Prophesee SDK installer as a fallback. See
sdk_bootstrap.py.
"""
from __future__ import annotations

import sys

from sdk_bootstrap import activate
activate()

# ── Normal imports ────────────────────────────────────────────────────────────

import argparse
import os
import faulthandler
faulthandler.enable()

from camera_manager import CameraManager
from visualizer import EventVisualizer


class _FileCameraStub:
    """Minimal stand-in for CameraManager when playing back a file.

    Provides the same interface that EventVisualizer reads but takes no
    action (no physical camera is attached).
    """

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.is_raw_recording = False

    def get_all_bias_info(self):  return []
    def set_bias(self, *_):       return False
    def get_bias(self, *_):       return None
    def set_roi(self, *_):        return False
    def clear_roi(self):          pass
    def start_raw_recording(self, *_): return False
    def stop_raw_recording(self): pass

    def has_antiflicker(self):             return False
    def get_antiflicker_settings(self):    return None
    def set_antiflicker(self, *_, **__):   return False
    def has_erc(self):                     return False
    def get_erc_settings(self):            return None
    def set_erc(self, *_, **__):           return False
    def has_trail_filter(self):            return False
    def get_trail_filter_settings(self):   return None
    def set_trail_filter(self, *_, **__):  return False
    def has_event_rate_filter(self):           return False
    def get_event_rate_filter_settings(self):  return None
    def set_event_rate_filter(self, *_, **__): return False
    def active_filter_tags(self):              return []
    def close(self):              pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Prophesee event camera viewer with bias, ROI, and recording."
    )
    # ── File playback ──
    p.add_argument(
        "--input", metavar="FILE",
        help="HDF5 or RAW file to replay instead of opening a live camera.",
    )
    p.add_argument(
        "--playlist", metavar="DIR",
        help="Folder of HDF5 files to play in sequence. "
             "Use [ / ] to navigate between files.",
    )
    p.add_argument(
        "--speed", type=float, default=1.0, metavar="X",
        help="Playback speed multiplier (1.0 = real time, 0 = as fast as possible). "
             "Only used with --input or --playlist. (default: 1.0)",
    )
    # ── Live camera ──
    p.add_argument(
        "--serial", default="",
        help="Camera serial number (blank = first found). Ignored with --input.",
    )
    # ── Common ──
    p.add_argument(
        "--slice-us", type=int, default=10_000, metavar="US",
        help="Event slice duration in µs (default: 10000).",
    )
    p.add_argument(
        "--accum-us", type=int, default=20_000, metavar="US",
        help="Initial accumulation window for display in µs (default: 20000).",
    )
    p.add_argument(
        "--fps", type=int, default=30,
        help="Display frame rate (default: 30).",
    )
    p.add_argument(
        "--suite", metavar="FILE",
        help="JSON config file for automated bias-sweep suite. "
             "Press T in the viewer to start.",
    )
    p.add_argument(
        "--tracker-algo", default="MIL",
        help="Object tracking algorithm (default: MIL). Available: MIL, DaSiamRPN, Nano, Vit. "
             "Press K in the viewer to select a target.",
    )
    p.add_argument(
        "--virtual-cam", action="store_true",
        help="Also send the rendered view to a virtual webcam (v4l2loopback on Linux, "
             "OBS on Windows/macOS) so other apps can use it as a camera source.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.playlist:
        # ── Playlist mode ─────────────────────────────────────────────────────
        import glob
        from playlist import PlaylistIterator

        paths = sorted(
            glob.glob(os.path.join(args.playlist, "*.hdf5")) +
            glob.glob(os.path.join(args.playlist, "*.raw"))
        )
        if not paths:
            print(f"Error: no HDF5 or RAW files found in {args.playlist}", file=sys.stderr)
            return 1

        print(f"Playlist: {len(paths)} files from {args.playlist}")
        try:
            pl = PlaylistIterator(paths, delta_t_us=args.slice_us, replay_speed=args.speed)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        camera: CameraManager = _FileCameraStub(pl.width, pl.height)  # type: ignore[assignment]
        viz = EventVisualizer(
            camera,
            delta_t_us=args.slice_us,
            accumulation_us=args.accum_us,
            display_fps=args.fps,
            iterator=pl,
            file_mode=True,
            playlist=pl,
            tracker_algo=args.tracker_algo,
            virtual_cam=args.virtual_cam,
        )
        try:
            viz.run()
        except KeyboardInterrupt:
            pass
        finally:
            pl.close()

    elif args.input:
        # ── File playback mode ────────────────────────────────────────────────
        try:
            if args.input.lower().endswith(".raw"):
                from raw_reader import RawEventsIterator
                it = RawEventsIterator(
                    args.input, delta_t_us=args.slice_us, replay_speed=args.speed,
                    keep_alive_at_eof=True,  # so the seek bar can scrub back after EOF
                )
            else:
                from hdf5_reader import HDF5EventsIterator
                it = HDF5EventsIterator(args.input, delta_t_us=args.slice_us, replay_speed=args.speed)
        except Exception as exc:
            print(f"Error: could not open {args.input} — {exc}", file=sys.stderr)
            return 1

        camera: CameraManager = _FileCameraStub(it.width, it.height)  # type: ignore[assignment]
        viz = EventVisualizer(
            camera,
            delta_t_us=args.slice_us,
            accumulation_us=args.accum_us,
            display_fps=args.fps,
            iterator=it,
            file_mode=True,
            source_path=args.input,
            tracker_algo=args.tracker_algo,
            virtual_cam=args.virtual_cam,
        )
        try:
            viz.run()
        except KeyboardInterrupt:
            pass
        finally:
            it.close()

    else:
        # ── Live camera mode ──────────────────────────────────────────────────
        camera = CameraManager()
        try:
            print("Opening camera …")
            camera.open(args.serial)
            print(f"Camera ready: {camera.width}×{camera.height} px")
        except Exception as exc:
            print(f"Error: could not open camera — {exc}", file=sys.stderr)
            return 1

        suite = None
        if args.suite:
            from suite_runner import SuiteRunner
            try:
                suite = SuiteRunner(args.suite, camera)
            except Exception as exc:
                print(f"Error loading suite: {exc}", file=sys.stderr)
                camera.close()
                return 1

        viz = EventVisualizer(
            camera,
            delta_t_us=args.slice_us,
            accumulation_us=args.accum_us,
            display_fps=args.fps,
            suite_runner=suite,
            tracker_algo=args.tracker_algo,
            virtual_cam=args.virtual_cam,
        )
        try:
            viz.run()
        except KeyboardInterrupt:
            pass
        finally:
            camera.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
