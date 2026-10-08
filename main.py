#!/usr/bin/env python3
"""Prophesee event camera viewer — bias, ROI, and recording controls.

Live camera:
    python main.py [--serial <SN>] [--slice-us 10000] [--fps 30] [--accum-us 20000]

Live camera over TCP (e.g. a Prophesee Onboard running tcp_event_streamer):
    python main.py --tcp 169.254.10.10:9000

Several live cameras at once (repeat --serial / --tcp, optionally NAME=...):
    python main.py --serial evk4= --tcp genx320=192.168.1.20:9000 --tcp onboard=169.254.10.10:9000

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
import re
import faulthandler
faulthandler.enable()

from camera_manager import CameraManager
from visualizer import EventVisualizer, MultiCameraViewer


class _FileCameraStub:
    """Minimal stand-in for CameraManager when playing back a file, or when
    streaming a live camera over TCP (--tcp) — neither case has local HAL
    access to a physical device.

    Provides the same interface that EventVisualizer reads but takes no
    action.
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


class _RemoteRawCameraStub(_FileCameraStub):
    """As _FileCameraStub, but RAW recording (R key) is carried out on the
    streaming device itself and the file copied back afterwards — see
    NetworkEventsIterator.start_remote_raw()."""

    def __init__(self, it) -> None:
        super().__init__(it.width, it.height)
        self._it = it

    def start_raw_recording(self, path: str) -> bool:
        self.is_raw_recording = self._it.start_remote_raw(path)
        return self.is_raw_recording

    def stop_raw_recording(self) -> None:
        self._it.stop_remote_raw()
        self.is_raw_recording = False


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
        "--serial", action="append", nargs="?", const="", metavar="[NAME=][SN]",
        help="Camera serial number — a bare --serial (or NAME=) means whichever "
             "camera is plugged in, as does omitting it when no --tcp is given. "
             "Ignored with --input. Repeat, and/or combine with --tcp, to open "
             "several cameras at once — R / H then record on all of them together, "
             "and NAME (default: the serial number) labels that camera's window "
             "and recording files.",
    )
    p.add_argument(
        "--tcp", action="append", metavar="[NAME=]HOST:PORT",
        help="Connect to a camera streamed over TCP (a Prophesee Onboard running "
             "tcp_event_streamer, or a GenX320/other camera running "
             "genx320_streamer.py or genx320_streamer_native — same wire protocol, "
             "any of them work) instead of a local camera or file, e.g. "
             "--tcp 169.254.10.10:9000. See onboard_streamer/tcp_event_streamer.cpp, "
             "genx320_streamer.py, and genx320_streamer_native/. Repeatable, with "
             "an optional NAME (default: the host) — see --serial.",
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


def _split_name(spec: str) -> tuple[str, str]:
    """"NAME=VALUE" → (NAME, VALUE); a bare "VALUE" → ("", VALUE)."""
    name, sep, value = spec.partition("=")
    return (name, value) if sep else ("", spec)


def _run_multi(args: argparse.Namespace, serials: list, tcp_addrs: list) -> int:
    """Several live cameras in one instance — see MultiCameraViewer."""
    if args.suite:
        print("Error: --suite only supports a single camera.", file=sys.stderr)
        return 1

    sessions: list[tuple[str, object, object]] = []  # (label, camera, TCP iterator or None)

    def add(name: str, fallback: str, camera, it=None) -> None:
        label = re.sub(r"[^A-Za-z0-9_]+", "-", name or fallback).strip("-") or "cam"
        if any(label == s[0] for s in sessions):
            label = f"{label}-{len(sessions) + 1}"
        sessions.append((label, camera, it))

    def close_all() -> None:
        for _, camera, it in sessions:
            if it is None:
                camera.close()
                continue
            try:
                # A recording stopped by quitting still has to be copied over.
                it.finish_remote_raw()
            except KeyboardInterrupt:
                pass
            it.close()

    try:
        for name, serial in serials:
            camera = CameraManager()
            print(f"Opening camera {serial or '(first found)'} …")
            camera.open(serial)
            print(f"Camera ready: {camera.width}×{camera.height} px")
            if not serial:
                try:
                    serial = camera.device.get_i_hw_identification().get_serial()
                except Exception:
                    pass
            add(name, serial, camera)
        if tcp_addrs:
            from network_reader import NetworkEventsIterator
        for name, addr in tcp_addrs:
            it = NetworkEventsIterator(addr)
            camera = _RemoteRawCameraStub(it) if it.supports_remote_raw else _FileCameraStub(it.width, it.height)
            add(name, addr.rpartition(":")[0], camera, it)
    except Exception as exc:
        print(f"Error: could not open every camera — {exc}", file=sys.stderr)
        close_all()
        return 1

    vizs = [
        EventVisualizer(
            camera,
            delta_t_us=args.slice_us,
            accumulation_us=args.accum_us,
            display_fps=args.fps,
            iterator=it,
            tracker_algo=args.tracker_algo,
            virtual_cam=args.virtual_cam and i == 0,  # one virtual webcam: the first camera
            label=label,
        )
        for i, (label, camera, it) in enumerate(sessions)
    ]
    print("Cameras: " + ", ".join(f"[{i + 1}] {s[0]}" for i, s in enumerate(sessions))
          + " — R / H record on all, Tab or 1-9 selects one for everything else.")
    try:
        MultiCameraViewer(vizs).run()
    except KeyboardInterrupt:
        pass
    finally:
        close_all()
    return 0


def main() -> int:
    args = parse_args()
    serials = [_split_name(s) for s in args.serial or []]
    tcp_addrs = [_split_name(a) for a in args.tcp or []]

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

    elif len(serials) + len(tcp_addrs) > 1:
        return _run_multi(args, serials, tcp_addrs)

    elif tcp_addrs:
        # ── Live camera over TCP (e.g. Prophesee Onboard) ───────────────────────
        from network_reader import NetworkEventsIterator
        tcp_addr = tcp_addrs[0][1]
        try:
            it = NetworkEventsIterator(tcp_addr)
        except Exception as exc:
            print(f"Error: could not connect to {tcp_addr} — {exc}", file=sys.stderr)
            return 1

        if it.supports_remote_raw:
            camera: CameraManager = _RemoteRawCameraStub(it)  # type: ignore[assignment]
        else:
            camera = _FileCameraStub(it.width, it.height)  # type: ignore[assignment]
        viz = EventVisualizer(
            camera,
            delta_t_us=args.slice_us,
            accumulation_us=args.accum_us,
            display_fps=args.fps,
            iterator=it,
            tracker_algo=args.tracker_algo,
            virtual_cam=args.virtual_cam,
        )
        try:
            viz.run()
        except KeyboardInterrupt:
            pass
        finally:
            try:
                # A recording stopped by quitting still has to be copied over.
                it.finish_remote_raw()
            except KeyboardInterrupt:
                pass
            it.close()

    else:
        # ── Live camera mode ──────────────────────────────────────────────────
        camera = CameraManager()
        try:
            print("Opening camera …")
            camera.open(serials[0][1] if serials else "")
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
