#!/usr/bin/env python3
"""Headless RAW recording for unattended/remote-controlled deployments (e.g. a
GenX320 on a Raspberry Pi with no display attached).

Opens the camera and starts RAW recording immediately, then blocks until it
receives SIGINT/SIGTERM (systemd's default stop signal) — no OpenCV window,
no Tk panels, so it runs fine with no display or X server. Meant to be
wrapped by a systemd service and started/stopped remotely over SSH; see the
"Headless recording (Raspberry Pi)" section of the README.

Linux-only (uses signal.pause()).
"""
from __future__ import annotations

import sys

from sdk_bootstrap import activate
activate()

import argparse
import json
import os
import signal
from datetime import datetime

from camera_manager import CameraManager


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--serial", default="",
        help="Camera serial number (blank = first found).",
    )
    p.add_argument(
        "--output-dir", default=".", metavar="DIR",
        help="Directory to write the RAW recording into (default: current directory).",
    )
    p.add_argument(
        "--bias-file", metavar="FILE",
        help="Optional JSON file of {bias_name: value} to apply before recording starts.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    camera = CameraManager()
    print("Opening camera …", flush=True)
    try:
        camera.open(args.serial)
    except Exception as exc:
        print(f"Error: could not open camera — {exc}", file=sys.stderr)
        return 1
    print(f"Camera ready: {camera.width}x{camera.height} px", flush=True)

    if args.bias_file:
        with open(args.bias_file) as f:
            biases = json.load(f)
        for name, value in biases.items():
            if camera.set_bias(name, value):
                print(f"  {name} = {value}", flush=True)
            else:
                print(f"[WARN] failed to set bias {name}={value}", file=sys.stderr)

    path = os.path.join(args.output_dir, f"recording_{datetime.now():%Y%m%d_%H%M%S}.raw")
    if not camera.start_raw_recording(path):
        print("Error: failed to start RAW recording", file=sys.stderr)
        camera.close()
        return 1
    print(f"Recording to {path}", flush=True)
    print("Waiting for SIGINT/SIGTERM to stop …", flush=True)

    stop = False

    def _handle_stop(signum, frame) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    # start_raw_recording() only arms the HAL to log; nothing actually reaches
    # disk until the device's event stream is drained. main.py's GUI loop does
    # this via a background thread pumping camera.get_iterator() even during
    # plain RAW recording — headless mode has to pump it too, here in the
    # main thread since there's no display loop to do it for us.
    for _ in camera.get_iterator():
        if stop:
            break

    print("Stopping …", flush=True)
    camera.close()  # stops RAW recording
    print(f"Done: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
