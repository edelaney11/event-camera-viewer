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

If this process itself becomes the bottleneck (profile it — under real
load, decoding events into (x, y, p, t) is genuinely CPU-heavy, and
Python's GIL serializes that against this script's own network-send loop
on a single core even on a multi-core Pi), see genx320_streamer_native/ — a
C++ equivalent speaking the exact same wire protocol, so network_reader.py
and main.py --tcp need no changes to use it instead. (An earlier attempt at
a --raw mode here, relaying undecoded bytes for the client to decode
instead, turned out to be a dead end: OpenEB's RAW-file reading
unconditionally seeks to end-of-file to measure total size, which a live
pipe can never support — confirmed by testing, not just reasoning about it.)
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
from hdf5_reader import EVENT_CD_DTYPE

_HEADER_FMT = "<4sIHH"
_MAGIC = b"PECD"
_PROTOCOL_VERSION = 1
_COUNT_FMT = "<I"

# Client → server commands — see network_reader.NetworkEventsIterator.set_recording(),
# the only sender. Toggles between this tool's two operating modes: live
# view (default — discard backlog on reconnect, see queue.clear() below) and
# lossless capture (preserve backlog across a reconnect instead), for
# whatever span the client is actually recording what it receives.
_CMD_STOP_RECORDING = 0x00
_CMD_START_RECORDING = 0x01

# Cap on how much backlog gets coalesced into a single network message —
# bounds worst-case message size without meaningfully limiting how much a
# burst can be coalesced. Matches tcp_event_streamer.cpp's kMaxEventsPerMessage.
_MAX_EVENTS_PER_MESSAGE = 200_000

_STATUS_INTERVAL_S = 10.0
_ACCEPT_POLL_S = 0.5
_POP_POLL_S = 0.2


class EventQueue:
    """Thread-safe hand-off between the camera pump thread (producer) and the
    socket-writer loop (consumer). Enforces one of two caps depending on
    recording_event (shared with main()'s command handling):

      - Not recording (live view): live_cap, a deliberately small bound.
        This tool feeds a live viewer, and if the camera produces events
        faster than the link to the client can carry (e.g. a GenX320 in a
        busy scene over Wi-Fi), buffering up to max_queued_events just
        delays the problem — the viewer falls further and further behind
        "live" until it does start dropping anyway, except now from a
        multi-second-deep backlog instead of a shallow one. A small cap
        means the drop starts immediately and lag stays bounded, which is
        the right trade-off when nothing is being recorded.
      - Recording: max_queued_events. Here dropping is a real, unwanted
        loss, so by default (0) there is no cap at all — every event is
        kept and eventually delivered no matter how large the backlog
        grows, because recording means the caller needs all of it, not a
        bounded approximation of it. A nonzero value turns this into an OOM
        circuit breaker instead (dropping, loudly, rather than growing
        memory without bound) — a deliberate trade of completeness for a
        memory ceiling, not the default.
    """

    def __init__(self, max_queued_events: int, live_cap: int, recording_event: threading.Event) -> None:
        self._max = max_queued_events
        self._live_cap = live_cap
        self._recording_event = recording_event
        self._cv = threading.Condition()
        self._batches: deque[np.ndarray] = deque()
        self._queued = 0
        self._dropped = 0

    def push(self, batch: np.ndarray) -> None:
        # Empty batches ("no new events, but time has passed") are forwarded
        # too, not dropped — EventsIterator yields these periodically in
        # delta_t mode, and the client-side frame generator relies on them
        # for regular timing the same way it does from a local camera (see
        # visualizer.py's _event_loop, which calls process_events(evs) on
        # every batch unconditionally). Dropping them here would let a long
        # idle period collapse into a single large timestamp jump once
        # events do resume, instead of many small, expected ones.
        with self._cv:
            self._batches.append(batch)
            self._queued += batch.size
            cap = self._max if self._recording_event.is_set() else self._live_cap
            while cap > 0 and self._queued > cap and self._batches:
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

    def clear(self) -> int:
        """Discards any queued backlog and returns how many events were
        dropped. Used when a new client connects — see the call site for why
        this tool deliberately does NOT preserve backlog across connections
        (unlike tcp_event_streamer.cpp, which is built for lossless capture)."""
        with self._cv:
            dropped = self._queued
            self._batches.clear()
            self._queued = 0
            return dropped

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


def _read_commands(client_sock: socket.socket, recording_event: threading.Event) -> None:
    """Runs on its own thread for the life of one connection, reading 1-byte
    commands from the client (network_reader.NetworkEventsIterator.set_recording())
    concurrently with the main thread's send loop on the same socket — a TCP
    connection's two directions are independent, so this needs no
    coordination with the sender beyond the shared recording_event.
    recording_event is process-level, not per-connection: once the client
    tells us it's recording, backlog keeps getting preserved (not cleared on
    reconnect, see main()) across any later disconnect/reconnect, until it
    explicitly tells us to stop — a dropped connection alone is not
    distinguished from a deliberate pause, since either way losing data
    mid-recording is the wrong default. Returns when the client disconnects
    or the socket errors; does not touch the event queue itself."""
    try:
        while True:
            data = client_sock.recv(1)
            if not data:
                return
            if data[0] == _CMD_START_RECORDING:
                recording_event.set()
                print("  Recording started by client — switching to lossless capture "
                      "(backlog preserved across reconnects).", flush=True)
            elif data[0] == _CMD_STOP_RECORDING:
                recording_event.clear()
                print("  Recording stopped by client — back to live view "
                      "(backlog discarded on reconnect).", flush=True)
    except OSError:
        return


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
        "--max-queued-events", type=int, default=0,
        help="Cap on backlog while recording (default: 0 = unlimited — every event is kept and "
             "eventually delivered, no matter how far behind the network link falls, because "
             "recording means you need all of it). This trades away the OOM protection a nonzero "
             "value would give you: if the camera sustainably outpaces the link for long enough, "
             "memory usage grows without bound and the process can eventually be killed for OOM, "
             "which would lose the whole recording rather than just the overflow. Pass a nonzero "
             "value (e.g. 20000000, several hundred MB) to accept that smaller, bounded, loud data "
             "loss instead of the OOM risk — or fix the real imbalance with --max-rate, so the "
             "backlog this is meant to protect against never has to grow large in the first place. "
             "Not used while just live-viewing — see --live-queue-cap — and see EventQueue's docstring.",
    )
    p.add_argument(
        "--live-queue-cap", type=int, default=500_000,
        help="Cap on queued-but-unsent events while NOT recording (default: 500,000, a few MB). "
             "Deliberately much smaller than --max-queued-events: if the camera produces events "
             "faster than the link to the client can carry (e.g. a GenX320 in a busy scene over "
             "Wi-Fi), this keeps the live view's lag bounded by dropping the oldest backlog "
             "promptly, instead of growing a multi-second backlog before dropping anyway. If you "
             "see repeated 'DATA LOSS' messages while just viewing, that's expected under sustained "
             "overload — see --max-rate to address it at the source instead.",
    )
    p.add_argument(
        "--max-rate", type=int, default=0, metavar="KEV_S",
        help="Cap event production at the sensor itself, in kilo-events/sec (0 = unlimited, the "
             "default), via the camera's on-chip Event Rate Controller (ERC) if it has one. WARNING: "
             "events above this rate are never generated at all — permanent data loss at the source, "
             "not lag. The right tool when the camera can sustainably produce more events than the "
             "network link (e.g. Wi-Fi) can carry — caps the problem before it ever reaches the "
             "queue, instead of coping with it downstream via --live-queue-cap.",
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

    if args.max_rate > 0:
        if camera.set_erc(True, args.max_rate * 1000):
            print(f"Max event rate limited to {args.max_rate} kEv/s (on-chip ERC)", flush=True)
        else:
            print(f"[WARN] --max-rate requested but this camera/plugin has no ERC facility "
                  f"(camera.has_erc()={camera.has_erc()}) — ignoring, event rate is unlimited.",
                  file=sys.stderr)

    try:
        listen_sock = _make_listen_socket(args.bind, args.port)
    except OSError as exc:
        print(f"Error: could not listen on {args.bind}:{args.port} — {exc}", file=sys.stderr)
        camera.close()
        return 1
    print(f"Listening on {args.bind}:{args.port}", flush=True)

    stop_event = threading.Event()
    # None until the first client connects — see the "pump_thread is None"
    # branch below. Not started at process launch: pulling (and queuing)
    # events from the camera before anyone is listening is what produced a
    # multi-million-event backlog in testing, which then got dumped on the
    # first connecting client all at once and crashed it.
    pump_thread: threading.Thread | None = None
    # Process-level, not per-connection — see _read_commands()'s docstring
    # for why it deliberately survives a disconnect/reconnect. Created
    # before the queue, which reads it to pick which cap applies.
    recording_event = threading.Event()
    queue = EventQueue(args.max_queued_events, args.live_queue_cap, recording_event)

    def _handle_stop(signum, frame) -> None:
        stop_event.set()

    # Explicit handlers (rather than relying on Python's default SIGINT
    # behavior) so SIGTERM — what systemd sends on `systemctl stop` — also
    # triggers the graceful shutdown path below, same as record_headless.py.
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    header = struct.pack(_HEADER_FMT, _MAGIC, _PROTOCOL_VERSION, camera.width, camera.height)
    print("Waiting for a client before pulling any events from the camera …", flush=True)

    try:
        while not stop_event.is_set():
            try:
                client_sock, client_addr = listen_sock.accept()
            except socket.timeout:
                continue
            client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            if pump_thread is None:
                # Prime the generator on the main thread so the SDK's native
                # __enter__ (inside EventsIterator.__iter__) runs here, not
                # on the background thread — same convention as
                # visualizer.py's own live-camera loop. Only done once: later
                # reconnects reuse the already-running pump/queue.
                primed_iter = iter(camera.get_iterator(delta_t_us=args.slice_us))
                pump_thread = threading.Thread(
                    target=_pump_events, args=(primed_iter, queue, stop_event), daemon=True
                )
                pump_thread.start()
                print("First client connected — camera event stream started.", flush=True)

            # Discard any backlog that piled up while no client was connected
            # (including before the very first connection — the camera has
            # been producing events since camera.open(), with nobody to
            # receive them) UNLESS the client told us (via set_recording())
            # it's actively recording what it receives — in which case this
            # is a reconnect mid-recording, and losing the gap would defeat
            # the point of recording at all, so the backlog is preserved
            # instead (bounded only by --max-queued-events, same as
            # tcp_event_streamer.cpp). Outside of that, a stale backlog
            # delivered all at once forces the client's frame generator to
            # synchronously render every elapsed time-slice across that
            # whole span in one burst — the input shape that crashed the
            # viewer in testing — so plain live view defaults to discarding it.
            if recording_event.is_set():
                stale = 0
                print(f"Client connected: {client_addr[0]}  (recording still active — backlog preserved)", flush=True)
            else:
                stale = queue.clear()
                queue.take_dropped()  # also reset — stale already covers it
                msg = f"Client connected: {client_addr[0]}"
                if stale:
                    msg += f"  (discarded {stale} stale backlogged events — starting live)"
                print(msg, flush=True)

            try:
                client_sock.sendall(header)
            except OSError:
                print("Failed to send handshake — dropping client.", file=sys.stderr)
                client_sock.close()
                continue

            cmd_thread = threading.Thread(target=_read_commands, args=(client_sock, recording_event), daemon=True)
            cmd_thread.start()

            client_ok = True
            last_status = time.monotonic()
            while client_ok and not stop_event.is_set():
                now = time.monotonic()
                if now - last_status >= _STATUS_INTERVAL_S:
                    last_status = now
                    print(f"  (status: {queue.queued_events()} events queued)", flush=True)
                dropped_now = queue.take_dropped()
                if dropped_now and recording_event.is_set():
                    print(f"  (DATA LOSS: {dropped_now} events dropped — sustained overload exceeded "
                          f"--max-queued-events; raise it, or reduce sensor activity, if this recurs)",
                          file=sys.stderr)
                elif dropped_now:
                    # Expected, not an error: the camera is producing events
                    # faster than this connection can carry them, and we're
                    # not recording — see --live-queue-cap's and --max-rate's
                    # help text for how to address it, rather than just the
                    # symptom.
                    print(f"  (falling behind: {dropped_now} oldest events dropped to stay live — "
                          f"not recording, so this is expected under sustained overload; see "
                          f"--max-rate to cap it at the source)", flush=True)

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
                # dtype=EVENT_CD_DTYPE is required here, not cosmetic:
                # np.concatenate() on an "aligned" structured dtype (ours has
                # explicit padding between p and t, offsets=[0,2,4,8],
                # itemsize=16 — see EVENT_CD_DTYPE) silently repacks its
                # *output* to the minimal packed layout (itemsize=14, no
                # padding) unless told otherwise, even though every input
                # array keeps the padded 16-byte layout. The result still
                # has x/y/p/t fields and looks correct in Python — only
                # .tobytes() reveals it's now 14 bytes/event, not the 16 the
                # wire protocol's `count` field and the client both assume,
                # desyncing the stream on every single message after it.
                # Forcing the dtype here keeps concatenate's output padded.
                batch = parts[0] if len(parts) == 1 else np.concatenate(parts, dtype=EVENT_CD_DTYPE)

                # Two sendall() calls, not one concatenated buffer: a numpy
                # array satisfies the buffer protocol, so handing it to
                # sendall() directly sends straight from its own memory —
                # .tobytes() would copy it into a new bytes object first,
                # and `header + batch.tobytes()` would copy it a *second*
                # time to build the concatenated buffer. For a large
                # coalesced message (up to 200,000 events, ~3.2MB) that's
                # two needless full-size memcpys per send, purely to save
                # one syscall — real CPU cost on a Pi, where this loop has
                # been observed pegging a full core well before the network
                # link (even gigabit Ethernet) was anywhere near its limit.
                try:
                    client_sock.sendall(struct.pack(_COUNT_FMT, batch.size))
                    client_sock.sendall(batch)
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
        if pump_thread is not None:
            pump_thread.join(timeout=2.0)
        camera.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
