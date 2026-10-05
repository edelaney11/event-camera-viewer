#!/usr/bin/env python3
"""Streams CD events from a local camera (e.g. a GenX320 on a Raspberry Pi)
over a raw TCP socket, for consumption by network_reader.py's
NetworkEventsIterator — the same client main.py already uses for a Prophesee
Onboard (--tcp host:port). Pure-Python equivalent of
onboard_streamer/tcp_event_streamer.cpp, built against this repo's own
camera_manager.py (metavision_hal/metavision_core via OpenEB) instead of the
Onboard's closed-source prophesee_driver SDK — the GenX320's Raspberry Pi
V4L2 plugin has no such driver to build against, but doesn't need one: the
same wire protocol works regardless of what is on the server side.

No display/GUI — safe to run headless over SSH, same as record_headless.py.

Wire protocol (all integers little-endian — matches
onboard_streamer/tcp_event_streamer.cpp and network_reader.py byte-for-byte):

    Handshake, sent once as soon as a client connects:
        char[4]  magic     b"PECD"
        uint32_t version   protocol version (currently 1)
        uint16_t width
        uint16_t height

    Then, repeated for the life of the connection:
        uint32_t count               number of events in this batch
        count * 16-byte event        x:u2, y:u2, p:i2, 2 reserved bytes, t:i8
                                      — byte-for-byte metavision_core's own
                                      EventCD numpy dtype, so batches from
                                      camera.get_iterator() are sent as-is
                                      via .tobytes(), no repacking needed.
"""
from __future__ import annotations

import sys

from sdk_bootstrap import activate
activate()

import argparse
import json
import signal
import socket
import struct
import threading
import time
from collections import deque

import numpy as np

from camera_manager import CameraManager

_HEADER_FMT = "<4sIHH"
_MAGIC = b"PECD"
_PROTOCOL_VERSION = 1
_COUNT_FMT = "<I"

# Cap on how much backlog gets coalesced into a single network message —
# bounds worst-case message size without meaningfully limiting how much a
# burst can be coalesced. Matches tcp_event_streamer.cpp's kMaxEventsPerMessage.
_MAX_EVENTS_PER_MESSAGE = 200_000

_STATUS_INTERVAL_S = 10.0
_ACCEPT_POLL_S = 0.5
_POP_POLL_S = 0.2


class EventQueue:
    """Thread-safe hand-off between the camera pump thread (producer) and the
    socket-writer loop (consumer). See tcp_event_streamer.cpp's EventQueue
    for the rationale this mirrors: max_queued_events is an OOM circuit
    breaker, NOT a lag/latency control — for lossless capture, backlog must
    be allowed to grow through a burst and drain later rather than be
    discarded. It only exists so a truly pathological, sustained overload
    degrades (by dropping, loudly) rather than exhausting memory.
    """

    def __init__(self, max_queued_events: int) -> None:
        self._max = max_queued_events
        self._cv = threading.Condition()
        self._batches: deque[np.ndarray] = deque()
        self._queued = 0
        self._dropped = 0

    def push(self, batch: np.ndarray) -> None:
        if batch.size == 0:
            return
        with self._cv:
            self._batches.append(batch)
            self._queued += batch.size
            while self._max > 0 and self._queued > self._max and self._batches:
                dropped = self._batches.popleft()
                self._queued -= dropped.size
                self._dropped += dropped.size
            self._cv.notify()

    def pop(self, timeout: float) -> np.ndarray | None:
        with self._cv:
            if not self._batches:
                self._cv.wait(timeout)
            if not self._batches:
                return None
            return self._batches.popleft()

    def try_pop(self) -> np.ndarray | None:
        """Non-blocking: pops one already-queued batch, if any. Used to
        opportunistically coalesce a backlog into fewer, larger sends."""
        with self._cv:
            if not self._batches:
                return None
            return self._batches.popleft()

    def queued_events(self) -> int:
        with self._cv:
            return self._queued

    def take_dropped(self) -> int:
        with self._cv:
            dropped, self._dropped = self._dropped, 0
            return dropped

    def __len__(self) -> int:
        with self._cv:
            return len(self._batches)


def _pump_events(primed_iter, queue: EventQueue, stop_event: threading.Event) -> None:
    """Runs on a background thread, draining the camera's iterator into
    `queue` for the life of the stream — independent of whether a client is
    currently connected, same as the camera's own CD callback in
    tcp_event_streamer.cpp. If the iterator ends (camera disconnected/error)
    or raises, that's fatal to the whole process, not just the current
    client — there is no camera left to serve."""
    try:
        for batch in primed_iter:
            if stop_event.is_set():
                break
            queue.push(batch)
    except Exception as exc:
        print(f"[ERROR] camera event loop stopped: {exc}", file=sys.stderr)
    finally:
        stop_event.set()


def _make_listen_socket(bind_addr: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(_ACCEPT_POLL_S)  # so Ctrl+C is noticed promptly even with no client ever connecting
    sock.bind((bind_addr, port))
    sock.listen(1)
    return sock


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=9000, help="TCP port to listen on (default: 9000).")
    p.add_argument("--bind", default="0.0.0.0", help="Address to bind the listening socket to.")
    p.add_argument("--serial", default="", help="Camera serial number (blank = first found).")
    p.add_argument(
        "--bias-file", metavar="FILE",
        help="Optional JSON file of {bias_name: value} to apply before streaming starts.",
    )
    p.add_argument(
        "--slice-us", type=int, default=10000,
        help="Event batch slice duration in microseconds, passed to the SDK's EventsIterator (default: 10000).",
    )
    p.add_argument(
        "--max-queued-events", type=int, default=20_000_000,
        help="OOM safety valve, not a lag control (default: ~20M events, several hundred MB). "
             "0 disables it (unbounded — risks an OOM crash instead of bounded, logged data loss "
             "under pathological overload). See the docstring of EventQueue.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    camera = CameraManager()
    print("Opening camera …", flush=True)
    try:
        camera.open(args.serial)
    except Exception as exc:
        print(f"Error: could not open camera — {exc}", file=sys.stderr)
        return 1
    print(f"Camera opened: {camera.width}x{camera.height}", flush=True)

    if args.bias_file:
        with open(args.bias_file) as f:
            biases = json.load(f)
        for name, value in biases.items():
            if camera.set_bias(name, value):
                print(f"  {name} = {value}", flush=True)
            else:
                print(f"[WARN] failed to set bias {name}={value}", file=sys.stderr)

    try:
        listen_sock = _make_listen_socket(args.bind, args.port)
    except OSError as exc:
        print(f"Error: could not listen on {args.bind}:{args.port} — {exc}", file=sys.stderr)
        camera.close()
        return 1
    print(f"Listening on {args.bind}:{args.port}", flush=True)

    # Prime the generator on the main thread so the SDK's native __enter__
    # (inside EventsIterator.__iter__) runs here, not in the background
    # thread — same convention as visualizer.py's own live-camera loop.
    primed_iter = iter(camera.get_iterator(delta_t_us=args.slice_us))

    queue = EventQueue(args.max_queued_events)
    stop_event = threading.Event()

    def _handle_stop(signum, frame) -> None:
        stop_event.set()

    # Explicit handlers (rather than relying on Python's default SIGINT
    # behavior) so SIGTERM — what systemd sends on `systemctl stop` — also
    # triggers the graceful shutdown path below, same as record_headless.py.
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    pump_thread = threading.Thread(target=_pump_events, args=(primed_iter, queue, stop_event), daemon=True)
    pump_thread.start()

    header = struct.pack(_HEADER_FMT, _MAGIC, _PROTOCOL_VERSION, camera.width, camera.height)

    try:
        while not stop_event.is_set():
            try:
                client_sock, client_addr = listen_sock.accept()
            except socket.timeout:
                continue
            client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            backlog = queue.queued_events()
            msg = f"Client connected: {client_addr[0]}"
            if backlog:
                msg += f"  ({backlog} backlogged events queued since the last connection — delivering those first, oldest first)"
            print(msg, flush=True)
            dropped = queue.take_dropped()
            if dropped:
                print(f"  ({dropped} events LOST while no client was connected — "
                      f"hit --max-queued-events; raise it if this recurs)", file=sys.stderr)

            try:
                client_sock.sendall(header)
            except OSError:
                print("Failed to send handshake — dropping client.", file=sys.stderr)
                client_sock.close()
                continue

            # Deliberately NOT clearing the queue here: any backlog that
            # built up while no client was connected gets delivered to this
            # client, oldest-first, rather than silently discarded.
            client_ok = True
            last_status = time.monotonic()
            while client_ok and not stop_event.is_set():
                now = time.monotonic()
                if now - last_status >= _STATUS_INTERVAL_S:
                    last_status = now
                    print(f"  (status: {queue.queued_events()} events queued)", flush=True)
                dropped_now = queue.take_dropped()
                if dropped_now:
                    print(f"  (DATA LOSS: {dropped_now} events dropped — sustained overload exceeded "
                          f"--max-queued-events; raise it, or reduce sensor activity, if this recurs)",
                          file=sys.stderr)

                batch = queue.pop(_POP_POLL_S)
                if batch is None:
                    continue

                # Opportunistically coalesce whatever else is already queued
                # — never waits any longer for more data, just picks up a
                # backlog in one shot instead of one small message per
                # camera callback.
                parts = [batch]
                total = batch.size
                while total < _MAX_EVENTS_PER_MESSAGE:
                    extra = queue.try_pop()
                    if extra is None:
                        break
                    parts.append(extra)
                    total += extra.size
                batch = parts[0] if len(parts) == 1 else np.concatenate(parts)

                payload = struct.pack(_COUNT_FMT, batch.size) + batch.tobytes()
                try:
                    client_sock.sendall(payload)
                except OSError:
                    client_ok = False

            print("Client disconnected.", flush=True)
            client_sock.close()
    except KeyboardInterrupt:
        pass
    finally:
        print("Shutting down …", flush=True)
        stop_event.set()
        listen_sock.close()
        pump_thread.join(timeout=2.0)
        camera.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
