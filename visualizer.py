"""OpenCV-based event camera visualiser.

Controls
--------
Mouse drag   Draw and apply hardware ROI
R            Toggle RAW recording
H            Toggle HDF5 recording
B            Toggle bias control panel
C            Clear ROI (full sensor)
+  /  -      Increase / decrease accumulation time (5 ms steps)
Space        Pause / resume display
S            Save a PNG snapshot
K            Start / stop object tracking (drag to select target)
T            Start / abort bias-sweep suite (requires --suite)
N            Skip to next suite step
]            Next file in playlist
[            Previous file in playlist
Q / Esc      Quit
"""
from __future__ import annotations

import itertools
import sys
import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

import cv2
import tkinter as tk
import ttkbootstrap as ttk
import pyvirtualcam

import numpy as np

from metavision_sdk_core import PeriodicFrameGenerationAlgorithm, ColorPalette

from camera_manager import CameraManager
from hdf5_writer import HDF5Writer
from tracker import ObjectTracker

MAIN_WIN = "Event Camera Viewer"

_ACCUM_STEP_US = 5_000
_ACCUM_MIN_US = 5_000
_ACCUM_MAX_US = 200_000

_SPEED_STEPS = [0.0, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0]
_CENTER_ROI_FRACTIONS = [0.75, 0.5, 0.25]

_TK_THEME = "darkly"
_TK_ACCENT = "info"


def _slider_row(parent, label: str, var: tk.Variable, lo: float, hi: float, apply_cb, integer: bool = True) -> None:
    """A bold label + numeric entry + slider, all bound to a shared Tk variable.

    Typing in the entry (Enter/blur) and dragging the slider both call apply_cb();
    a variable trace keeps the entry text in sync with whichever one last changed it.
    """
    row = ttk.Frame(parent)
    row.pack(fill="x", padx=12, pady=6)

    top = ttk.Frame(row)
    top.pack(fill="x")
    ttk.Label(top, text=label, font=("Segoe UI", 10, "bold")).pack(side="left")
    entry = ttk.Entry(top, width=8, justify="right", font=("Segoe UI", 10))
    entry.pack(side="right")

    def _refresh_entry(*_args, force: bool = False) -> None:
        if not force and parent.winfo_toplevel().focus_get() is entry:
            return  # don't clobber the user mid-edit
        entry.delete(0, "end")
        entry.insert(0, f"{var.get():g}")

    _refresh_entry(force=True)
    var.trace_add("write", _refresh_entry)

    def _commit_entry(_event=None) -> None:
        try:
            value = float(entry.get().strip())
        except ValueError:
            _refresh_entry(force=True)
            return
        value = max(lo, min(hi, value))
        var.set(round(value) if integer else value)
        _refresh_entry(force=True)
        apply_cb()

    entry.bind("<Return>", _commit_entry)
    entry.bind("<FocusOut>", _commit_entry)

    def _on_scale(value_str: str) -> None:
        if integer:
            rounded = round(float(value_str))
            if rounded != var.get():
                var.set(rounded)
        apply_cb()

    ttk.Scale(row, variable=var, from_=lo, to=hi, orient="horizontal",
              bootstyle=_TK_ACCENT, command=_on_scale).pack(fill="x", pady=(6, 0))


def _toggle_row(parent, label: str, enabled_var: tk.BooleanVar, apply_cb) -> None:
    ttk.Checkbutton(
        parent, text=label, variable=enabled_var, command=apply_cb,
        bootstyle="round-toggle",
    ).pack(anchor="w", padx=12, pady=(14, 2))


# ── HUD colours — sampled from ttkbootstrap's "darkly" theme, so the OpenCV
# overlay and the Tk panels read as one consistent, modern design. ─────────────
_HUD_PANEL_BG  = (34, 34, 34)     # theme 'bg'      #222222
_HUD_TEXT      = (255, 255, 255)  # theme 'fg'      #ffffff
_HUD_TEXT_DIM  = (170, 170, 170)
_HUD_ACCENT    = (219, 152, 52)   # theme 'info'    #3498db
_HUD_REC       = (60, 76, 231)    # theme 'danger'  #e74c3c
_HUD_FILTER    = (140, 188, 0)    # theme 'success' #00bc8c
_HUD_ROI       = (18, 156, 243)   # theme 'warning' #f39c12
_HUD_PANEL_ALPHA = 0.65


def _alpha_panel(img: np.ndarray, x0: int, y0: int, x1: int, y1: int,
                  color=_HUD_PANEL_BG, alpha: float = _HUD_PANEL_ALPHA) -> None:
    """Blend a translucent filled rectangle into img in-place (a HUD panel)."""
    x0, y0 = max(x0, 0), max(y0, 0)
    x1, y1 = min(x1, img.shape[1]), min(y1, img.shape[0])
    if x1 <= x0 or y1 <= y0:
        return
    roi = img[y0:y1, x0:x1]
    overlay = np.full_like(roi, color, dtype=np.uint8)
    cv2.addWeighted(overlay, alpha, roi, 1 - alpha, 0, dst=roi)


def _rounded_bar(img: np.ndarray, x0: int, y0: int, x1: int, y1: int, color) -> None:
    """A filled pill/rounded-rect: rectangle body + circular end caps."""
    r = max((y1 - y0) // 2, 1)
    x1 = max(x1, x0 + 2 * r)
    cv2.rectangle(img, (x0 + r, y0), (x1 - r, y1), color, -1, cv2.LINE_AA)
    cv2.circle(img, (x0 + r, (y0 + y1) // 2), r, color, -1, cv2.LINE_AA)
    cv2.circle(img, (x1 - r, (y0 + y1) // 2), r, color, -1, cv2.LINE_AA)


def _pill_size(text: str, font_scale: float = 0.42, thickness: int = 1,
               pad_x: int = 9, pad_y: int = 5) -> tuple[int, int]:
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
    return tw + pad_x * 2, th + pad_y * 2


def _pill(img: np.ndarray, text: str, x: int, y: int, color,
          text_color=(20, 18, 16), font_scale: float = 0.42, thickness: int = 1,
          pad_x: int = 9, pad_y: int = 5) -> int:
    """Draw a rounded badge with text anchored at top-left (x, y). Returns its width."""
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
    w, h = tw + pad_x * 2, th + pad_y * 2
    _rounded_bar(img, x, y, x + w, y + h, color)
    cv2.putText(img, text, (x + pad_x, y + pad_y + th - 1),
                cv2.FONT_HERSHEY_SIMPLEX, font_scale, text_color, thickness, cv2.LINE_AA)
    return w


class EventVisualizer:
    def __init__(
        self,
        camera: CameraManager,
        delta_t_us: int = 10_000,
        accumulation_us: int = 20_000,
        display_fps: int = 30,
        iterator=None,
        file_mode: bool = False,
        suite_runner=None,
        playlist=None,
        tracker_algo: str = "MIL",
        virtual_cam: bool = False,
    ) -> None:
        self._cam = camera
        self._delta_t_us = delta_t_us
        self._iterator = iterator    # pre-built iterator for file playback
        self._file_mode = file_mode  # disables live-only controls when True
        self._suite = suite_runner   # optional SuiteRunner instance
        self._playlist = playlist    # optional PlaylistIterator instance
        self._accum_us = accumulation_us
        self._display_fps = display_fps

        # ── Virtual webcam (optional) ────────────────────────────────────────
        self._virtual_cam_enabled = virtual_cam
        self._vcam: Optional[pyvirtualcam.Camera] = None

        # ── Shared state (background → main thread) ───────────────────────────
        self._frame_lock = threading.Lock()
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_ts_us: int = 0
        self._event_rate: float = 0.0         # ev/s rolling average

        # ── HDF5 recording ────────────────────────────────────────────────────
        self._hdf5_lock = threading.Lock()
        self._hdf5_writer: Optional[HDF5Writer] = None
        self._hdf5_pending: list[np.ndarray] = []

        # ── ROI drag state ───────────────────────────────────────────────────
        self._roi_drawing = False
        self._roi_drag_start = (0, 0)
        self._roi_drag_rect: Optional[tuple[int, int, int, int]] = None  # live preview
        self._active_roi: Optional[tuple[int, int, int, int]] = None     # committed x0,y0,x1,y1
        self._center_roi_idx: int = -1  # index into _CENTER_ROI_FRACTIONS, or -1 if inactive

        # ── Tk panels: one hidden master root (styles/theme live here) plus a
        # Toplevel per visible panel — ttkbootstrap does not support creating
        # more than one ttk.Window() per process.
        self._tk_master: Optional[ttk.Window] = None
        self._tk_root: Optional[ttk.Toplevel] = None
        self._tk_vars: dict[str, tk.IntVar] = {}
        self._tk_suppress_cb: bool = False  # prevent feedback when syncing scales

        # ── Filter panel (anti-flicker, ERC, trail, event-rate) ──────────────
        self._filter_tk_root: Optional[ttk.Toplevel] = None
        self._filter_tk_vars: dict[str, tk.Variable] = {}

        # ── Object tracking ──────────────────────────────────────────────────
        self._tracker_algo = tracker_algo.upper()
        self._tracker: Optional[ObjectTracker] = None
        self._tracker_selecting: bool = False
        self._tracker_drag_start: Optional[tuple[int, int]] = None
        self._tracker_drag_rect: Optional[tuple[int, int, int, int]] = None  # x,y,w,h

        # ── Control flags ────────────────────────────────────────────────────
        self._running = True
        self._paused = False
        self._replay_speed: float = iterator.replay_speed if hasattr(iterator, "replay_speed") else 1.0

        # ── Frame generator (live mode) / time-surface (file mode) ──────────
        if not file_mode:
            self._frame_gen = PeriodicFrameGenerationAlgorithm(
                sensor_width=camera.width,
                sensor_height=camera.height,
                fps=display_fps,
                palette=ColorPalette.Dark,
            )
            self._frame_gen.set_accumulation_time_us(accumulation_us)
            self._frame_gen.set_output_callback(self._on_frame_cb)
            self._ts_surface = None
            self._pol_surface = None
        else:
            self._frame_gen = None
            self._ts_surface = np.full((camera.height, camera.width), -1, dtype=np.int64)
            self._pol_surface = np.zeros((camera.height, camera.width), dtype=np.int8)

    # ── Frame generation callback (background thread) ─────────────────────────

    def _on_frame_cb(self, ts_us: int, frame: np.ndarray) -> None:
        with self._frame_lock:
            self._latest_frame = frame.copy()
            self._latest_ts_us = ts_us

    # ── Background event loop ─────────────────────────────────────────────────

    def _event_loop(self, mv_gen) -> None:
        rate_window_us = 1_000_000
        rate_buf: deque[tuple[int, int]] = deque()
        rate_total: int = 0

        for evs in mv_gen:
            if not self._running:
                break
            if self._paused:
                continue

            n = len(evs)
            if n > 0:
                ts_end = int(evs["t"][-1])
                rate_buf.append((ts_end, n))
                rate_total += n
                cutoff = ts_end - rate_window_us
                while rate_buf and rate_buf[0][0] < cutoff:
                    _, popped = rate_buf.popleft()
                    rate_total -= popped
                with self._frame_lock:
                    self._event_rate = rate_total / (rate_window_us * 1e-6)
                    if self._ts_surface is not None:
                        self._latest_ts_us = ts_end

                with self._hdf5_lock:
                    if self._hdf5_writer is not None:
                        self._hdf5_pending.append(evs.copy())

                if self._suite is not None:
                    self._suite.receive_events(evs)

                if self._ts_surface is not None:
                    self._ts_surface[evs["y"], evs["x"]] = evs["t"]
                    self._pol_surface[evs["y"], evs["x"]] = evs["p"]

            if self._frame_gen is not None:
                self._frame_gen.process_events(evs)

        self._running = False

    # ── HDF5 helpers (called from main thread) ────────────────────────────────

    def _flush_hdf5(self) -> None:
        with self._hdf5_lock:
            if self._hdf5_writer is None or not self._hdf5_pending:
                return
            pending, self._hdf5_pending = self._hdf5_pending, []
            writer = self._hdf5_writer
        for batch in pending:
            writer.write(batch)

    def _start_hdf5(self) -> None:
        path = f"recording_{datetime.now():%Y%m%d_%H%M%S}.hdf5"
        writer = HDF5Writer(path, self._cam.width, self._cam.height)
        with self._hdf5_lock:
            self._hdf5_writer = writer
            self._hdf5_pending.clear()
        print(f"HDF5 recording started: {path}")

    def _stop_hdf5(self) -> None:
        with self._hdf5_lock:
            writer, self._hdf5_writer = self._hdf5_writer, None
            pending, self._hdf5_pending = self._hdf5_pending, []
        if writer is not None:
            for batch in pending:
                writer.write(batch)
            writer.close()

    # ── Bias panel (tkinter) ──────────────────────────────────────────────────

    def _ensure_tk_master(self) -> None:
        """Create the single hidden Tk root that all panel Toplevels attach to."""
        if self._tk_master is None:
            self._tk_master = ttk.Window(themename=_TK_THEME)
            self._tk_master.withdraw()

    def _open_bias_panel(self) -> None:
        if self._file_mode:
            print("[INFO] Bias control not available in file playback mode.", file=sys.stderr)
            return
        if self._tk_root is not None:
            return
        biases = self._cam.get_all_bias_info()
        if not biases:
            print("[INFO] No biases available on this camera.", file=sys.stderr)
            return

        self._ensure_tk_master()
        root = ttk.Toplevel(title="Bias Controls", resizable=(True, False))
        root.protocol("WM_DELETE_WINDOW", self._close_bias_panel)

        ttk.Label(root, text="0 = factory default", bootstyle="secondary",
                  font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=(10, 2))

        self._tk_vars.clear()
        for b in biases:
            var = tk.IntVar(value=b.value)
            self._tk_vars[b.name] = var

            def _make_cb(name: str):
                def _cb() -> None:
                    if not self._tk_suppress_cb:
                        self._cam.set_bias(name, self._tk_vars[name].get())
                return _cb

            _slider_row(root, b.name, var, b.min_val, b.max_val, _make_cb(b.name))

        self._tk_root = root

    def _close_bias_panel(self) -> None:
        if self._tk_root is not None:
            try:
                self._tk_root.destroy()
            except Exception:
                pass
            self._tk_root = None
        self._tk_vars.clear()

    def _pump_tk(self) -> None:
        """Pump the shared Tk event loop and sync bias slider values from the camera."""
        if self._tk_master is None:
            return
        try:
            # Sync sliders only while a suite is actively changing biases
            if self._tk_root is not None and self._suite is not None and self._suite.is_active:
                self._tk_suppress_cb = True
                for name, var in self._tk_vars.items():
                    current = self._cam.get_bias(name)
                    if current is not None and var.get() != current:
                        var.set(current)
                self._tk_suppress_cb = False
            self._tk_master.update()
        except tk.TclError:
            # Master window was destroyed unexpectedly
            self._tk_master = None
            self._tk_root = None
            self._tk_vars.clear()
            self._filter_tk_root = None
            self._filter_tk_vars.clear()

    # ── Filter panel (tkinter) ────────────────────────────────────────────────

    def _open_filter_panel(self) -> None:
        if self._file_mode:
            print("[INFO] Noise/rate filters not available in file playback mode.", file=sys.stderr)
            return
        if self._filter_tk_root is not None:
            return

        cam = self._cam
        sections = [cam.has_antiflicker(), cam.has_erc(), cam.has_trail_filter(), cam.has_event_rate_filter()]
        if not any(sections):
            print("[INFO] No noise/rate filters available on this camera.", file=sys.stderr)
            return

        self._ensure_tk_master()
        root = ttk.Toplevel(title="Noise / Rate Filters", resizable=(True, False))
        root.protocol("WM_DELETE_WINDOW", self._close_filter_panel)
        self._filter_tk_vars.clear()

        # ── Anti-flicker ──
        if cam.has_antiflicker():
            s = cam.get_antiflicker_settings()
            enabled = tk.BooleanVar(value=s["enabled"])
            min_f = tk.IntVar(value=s["min_freq"])
            max_f = tk.IntVar(value=s["max_freq"])
            duty = tk.DoubleVar(value=s["duty_cycle"])
            self._filter_tk_vars.update({
                "afk_enabled": enabled, "afk_min_freq": min_f, "afk_max_freq": max_f, "afk_duty": duty,
            })

            def _apply(_e=None):
                cam.set_antiflicker(enabled.get(), min_f.get(), max_f.get(), duty.get())

            _toggle_row(root, "Anti-Flicker", enabled, _apply)
            lo, hi = s["freq_bounds"]
            _slider_row(root, "min_freq (Hz)", min_f, lo, hi, _apply)
            _slider_row(root, "max_freq (Hz)", max_f, lo, hi, _apply)
            dlo, dhi = s["duty_cycle_bounds"]
            _slider_row(root, "duty_cycle", duty, dlo, dhi, _apply, integer=False)
            ttk.Separator(root).pack(fill="x", padx=12, pady=8)

        # ── Event Rate Controller ──
        if cam.has_erc():
            s = cam.get_erc_settings()
            enabled = tk.BooleanVar(value=s["enabled"])
            rate = tk.IntVar(value=s["rate_events_per_sec"])
            self._filter_tk_vars.update({"erc_enabled": enabled, "erc_rate": rate})

            def _apply(_e=None):
                cam.set_erc(enabled.get(), rate.get())

            _toggle_row(root, "Event Rate Controller (ERC)", enabled, _apply)
            lo, hi = s["rate_bounds"]
            _slider_row(root, "target rate (ev/s)", rate, lo, hi, _apply)
            ttk.Separator(root).pack(fill="x", padx=12, pady=8)

        # ── Trail filter ──
        if cam.has_trail_filter():
            s = cam.get_trail_filter_settings()
            enabled = tk.BooleanVar(value=s["enabled"])
            threshold = tk.IntVar(value=s["threshold"])
            self._filter_tk_vars.update({"trail_enabled": enabled, "trail_threshold": threshold})

            def _apply(_e=None):
                cam.set_trail_filter(enabled.get(), threshold.get())

            _toggle_row(root, "Event Trail Filter", enabled, _apply)
            lo, hi = s["threshold_bounds"]
            _slider_row(root, "threshold", threshold, lo, hi, _apply)
            ttk.Separator(root).pack(fill="x", padx=12, pady=8)

        # ── Event rate activity filter ──
        if cam.has_event_rate_filter():
            s = cam.get_event_rate_filter_settings()
            enabled = tk.BooleanVar(value=s["enabled"])
            fields = ("lower_bound_start", "lower_bound_stop", "upper_bound_start", "upper_bound_stop")
            field_vars = {f: tk.IntVar(value=s[f]) for f in fields}
            self._filter_tk_vars["rate_enabled"] = enabled
            self._filter_tk_vars.update({f"rate_{f}": v for f, v in field_vars.items()})

            def _apply(_e=None):
                cam.set_event_rate_filter(enabled.get(), **{f: v.get() for f, v in field_vars.items()})

            _toggle_row(root, "Event Rate Activity Filter", enabled, _apply)
            for f in fields:
                lo, hi = s["bounds"][f]
                _slider_row(root, f, field_vars[f], lo, hi, _apply)

        self._filter_tk_root = root

    def _close_filter_panel(self) -> None:
        if self._filter_tk_root is not None:
            try:
                self._filter_tk_root.destroy()
            except Exception:
                pass
            self._filter_tk_root = None
        self._filter_tk_vars.clear()

    # ── Mouse callback (ROI drawing) ──────────────────────────────────────────

    def _mouse_cb(self, event: int, x: int, y: int, flags: int, param) -> None:
        if self._tracker_selecting:
            if event == cv2.EVENT_LBUTTONDOWN:
                self._tracker_drag_start = (x, y)
                self._tracker_drag_rect = None
            elif event == cv2.EVENT_MOUSEMOVE and self._tracker_drag_start:
                x0, y0 = self._tracker_drag_start
                self._tracker_drag_rect = (min(x0, x), min(y0, y), abs(x - x0), abs(y - y0))
            elif event == cv2.EVENT_LBUTTONUP and self._tracker_drag_start:
                x0, y0 = self._tracker_drag_start
                w, h = abs(x - x0), abs(y - y0)
                if w > 8 and h > 8:
                    bbox = (min(x0, x), min(y0, y), w, h)
                    with self._frame_lock:
                        frame = self._latest_frame.copy() if self._latest_frame is not None else None
                    if frame is not None:
                        if self._tracker is None:
                            self._tracker = ObjectTracker(self._tracker_algo)
                        self._tracker.initialize(frame, bbox)
                        print(f"[TRACK] Tracking started at {bbox}")
                self._tracker_selecting = False
                self._tracker_drag_start = None
                self._tracker_drag_rect = None
            return

        if event == cv2.EVENT_LBUTTONDOWN:
            self._roi_drawing = True
            self._roi_drag_start = (x, y)
            self._roi_drag_rect = None

        elif event == cv2.EVENT_MOUSEMOVE and self._roi_drawing:
            x0, y0 = self._roi_drag_start
            self._roi_drag_rect = (min(x0, x), min(y0, y), max(x0, x), max(y0, y))

        elif event == cv2.EVENT_LBUTTONUP and self._roi_drawing:
            self._roi_drawing = False
            x0, y0 = self._roi_drag_start
            rx0, ry0 = min(x0, x), min(y0, y)
            rx1, ry1 = max(x0, x), max(y0, y)
            self._roi_drag_rect = None

            # Ignore tiny drags (likely mis-clicks)
            if rx1 - rx0 > 8 and ry1 - ry0 > 8:
                # Clamp to sensor bounds
                W, H = self._cam.width, self._cam.height
                rx0, ry0 = max(0, rx0), max(0, ry0)
                rx1, ry1 = min(W, rx1), min(H, ry1)
                self._active_roi = (rx0, ry0, rx1, ry1)
                ok = self._cam.set_roi(rx0, ry0, rx1 - rx0, ry1 - ry0)
                status = "applied" if ok else "failed (no hardware ROI support)"
                print(f"ROI {rx0},{ry0}→{rx1},{ry1} {status}")

    # ── Overlay ───────────────────────────────────────────────────────────────

    def _draw_overlay(self, frame: np.ndarray, ts_us: int, rate: float, hdf5_rec: bool) -> np.ndarray:
        out = frame  # draw in-place — caller already owns this copy
        W, H = out.shape[1], out.shape[0]

        # Committed ROI
        if self._active_roi is not None:
            x0, y0, x1, y1 = self._active_roi
            cv2.rectangle(out, (x0, y0), (x1, y1), _HUD_ROI, 1, cv2.LINE_AA)
            if self._center_roi_idx >= 0:
                pct = int(_CENTER_ROI_FRACTIONS[self._center_roi_idx] * 100)
                _pill(out, f"CENTER {pct}%", x0 + 4, y0 + 4, _HUD_ROI)

        # Live drag preview
        if self._roi_drag_rect is not None:
            x0, y0, x1, y1 = self._roi_drag_rect
            cv2.rectangle(out, (x0, y0), (x1, y1), _HUD_ROI, 1, cv2.LINE_AA)

        raw_rec = self._cam.is_raw_recording

        # ── Top status bar: translucent panel, plain text left, badges right ──
        ts_str    = f"t={ts_us / 1_000_000:.3f}s"
        rate_str  = (f"{rate / 1_000_000:.2f} Mev/s" if rate >= 1e5
                     else f"{rate / 1_000:.1f} kev/s")
        accum_str = f"accum={self._accum_us // 1000}ms"
        if self._active_roi is None:
            roi_str = "ROI: full"
        elif self._center_roi_idx >= 0:
            roi_str = f"ROI: center {int(_CENTER_ROI_FRACTIONS[self._center_roi_idx] * 100)}%"
        else:
            roi_str = (f"ROI: {self._active_roi[0]},{self._active_roi[1]}"
                       f"-{self._active_roi[2]},{self._active_roi[3]}")

        _alpha_panel(out, 0, 0, W, 27)
        cv2.putText(out, f"{ts_str}   {rate_str}   {accum_str}   {roi_str}", (10, 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.48, _HUD_TEXT, 1, cv2.LINE_AA)

        # Blinking recording badges (pulses between full and dimmed colour)
        rec_pulse = (int(ts_us / 500_000) % 2 == 0)
        rec_color = _HUD_REC if rec_pulse else tuple(c // 2 for c in _HUD_REC)

        badges: list[tuple[str, tuple]] = []
        if raw_rec:
            badges.append(("RAW", rec_color))
        if hdf5_rec:
            badges.append(("HDF5", rec_color))
        if not self._file_mode:
            badges.extend((tag, _HUD_FILTER) for tag in self._cam.active_filter_tags())
        if self._file_mode:
            speed_label = "MAX" if self._replay_speed == 0 else f"x{self._replay_speed:g}"
            badges.append((speed_label, _HUD_ACCENT))

        bx, gap = W - 8, 6
        for text, color in reversed(badges):
            w, _ = _pill_size(text)
            bx -= w
            _pill(out, text, bx, 3, color)
            bx -= gap

        # Suite status bar (above the hint, when active)
        if self._suite is not None and (self._suite.is_active or self._suite.is_done):
            suite_y0 = H - 40
            _alpha_panel(out, 0, suite_y0, W, suite_y0 + 20)
            self._draw_suite_bar(out, suite_y0, W)

        # Playlist bar (above the hint, always shown in playlist mode)
        if self._playlist is not None:
            pl_y0 = H - 40
            _alpha_panel(out, 0, pl_y0, W, pl_y0 + 20)
            idx_str = f"[{self._playlist.current_index + 1}/{self._playlist.total}]"
            label = f"{idx_str}  {self._playlist.current_name}"
            cv2.putText(out, label, (10, pl_y0 + 14),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, _HUD_FILTER, 1, cv2.LINE_AA)
            nav = "[ prev   ] next"
            cv2.putText(out, nav, (W - 110, pl_y0 + 14),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.40, _HUD_TEXT_DIM, 1, cv2.LINE_AA)

        # Object tracker — bounding box, trail, and selection preview
        if self._tracker is not None and self._tracker.bbox is not None:
            x, y, w, h = self._tracker.bbox
            color = _HUD_REC if self._tracker.is_lost else _HUD_FILTER
            cv2.rectangle(out, (x, y), (x + w, y + h), color, 2, cv2.LINE_AA)
            label = "LOST" if self._tracker.is_lost else "TRACKING"
            _pill(out, label, x, max(y - 24, 4), color)
            trail = self._tracker.trail
            if len(trail) >= 2:
                pts = np.array(trail, dtype=np.int32).reshape((-1, 1, 2))
                cv2.polylines(out, [pts], False, color, 1, cv2.LINE_AA)

        if self._tracker_selecting:
            if self._tracker_drag_rect is not None:
                x, y, w, h = self._tracker_drag_rect
                cv2.rectangle(out, (x, y), (x + w, y + h), _HUD_FILTER, 2, cv2.LINE_AA)
            msg = "Draw box around target  -  K to cancel"
            mw, mh = _pill_size(msg, font_scale=0.48, pad_x=14, pad_y=8)
            mx, my = (W - mw) // 2, H // 2 - mh // 2
            _pill(out, msg, mx, my, _HUD_FILTER, font_scale=0.48, pad_x=14, pad_y=8)

        # Bottom hint bar
        if self._playlist is not None:
            hint = "[/:prev  ]:next  ,/.:speed  +/-:accum  Spc:pause  S:snap  Q:quit"
        elif self._file_mode:
            hint = ",/.:speed  +/-:accum  Spc:pause  S:snap  K:track  Q:quit"
        else:
            hint = "drag:ROI  O:centreROI  C:clrROI  R:RAW  H:HDF5  B:biases  F:filters  K:track  T:suite  +/-:accum  Spc:pause  Q:quit"
        _alpha_panel(out, 0, H - 22, W, H)
        cv2.putText(out, hint, (6, H - 7),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, _HUD_TEXT_DIM, 1, cv2.LINE_AA)

        return out

    def _draw_suite_bar(self, out: np.ndarray, y0: int, W: int) -> None:
        suite = self._suite
        s = suite.current_setting

        if suite.is_done:
            label = f"SUITE COMPLETE  ({suite.total} settings done)"
            cv2.putText(out, label, (10, y0 + 14),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, _HUD_FILTER, 1, cv2.LINE_AA)
            return

        idx_str = f"{suite.index + 1}/{suite.total}"
        name    = s.name if s else "?"
        dur     = suite.duration_s

        if suite.is_settling:
            elapsed_settle = time.monotonic() - suite._step_wall_start
            label = f"SUITE {idx_str}: {name}   settling {elapsed_settle:.1f}/{suite.settle_s:.1f}s"
            cv2.putText(out, label, (10, y0 + 14),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, _HUD_ROI, 1, cv2.LINE_AA)
        else:
            elapsed = suite.elapsed_record_s
            pct     = min(elapsed / dur, 1.0) if dur > 0 else 1.0
            bar_x0, bar_x1 = 8, W - 122
            bar_y0, bar_y1 = y0 + 5, y0 + 15
            r = (bar_y1 - bar_y0) // 2
            fill_x1 = bar_x0 + int((bar_x1 - bar_x0) * pct)

            _rounded_bar(out, bar_x0, bar_y0, bar_x1, bar_y1, (70, 68, 78))
            if fill_x1 > bar_x0:
                _rounded_bar(out, bar_x0, bar_y0, max(fill_x1, bar_x0 + 2 * r), bar_y1, _HUD_ACCENT)

            time_str = f"{elapsed:.1f}/{dur:.1f}s"
            cv2.putText(out, f"SUITE {idx_str}: {name}", (bar_x0 + 4, y0 + 14),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42, _HUD_TEXT, 1, cv2.LINE_AA)
            cv2.putText(out, time_str, (bar_x1 + 6, y0 + 14),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42, _HUD_TEXT, 1, cv2.LINE_AA)

    # ── Virtual webcam ─────────────────────────────────────────────────────────

    def _open_virtual_cam(self) -> None:
        try:
            self._vcam = pyvirtualcam.Camera(
                width=self._cam.width, height=self._cam.height,
                fps=self._display_fps, fmt=pyvirtualcam.PixelFormat.BGR,
            )
            print(f"Virtual camera active: {self._vcam.device}")
        except Exception as exc:
            print(
                f"[WARN] Could not start virtual camera: {exc}\n"
                "       Linux: sudo apt install v4l2loopback-dkms && sudo modprobe v4l2loopback\n"
                "       Windows/macOS: install and launch OBS at least once (ships a virtual cam).",
                file=sys.stderr,
            )
            self._vcam = None

    # ── Main run loop (must be called from main thread) ───────────────────────

    def run(self) -> None:
        if self._iterator is None:
            self._iterator = self._cam.get_iterator(self._delta_t_us)

        # Prime the generator on the main thread so the SDK's native __enter__
        # (inside EventsIterator.__iter__) runs here, not in the background thread.
        mv_gen = iter(self._iterator)
        try:
            first_evs = next(mv_gen)
        except StopIteration:
            return
        primed_gen = itertools.chain((first_evs,), mv_gen)

        evt_thread = threading.Thread(target=self._event_loop, args=(primed_gen,), daemon=True)
        evt_thread.start()

        cv2.namedWindow(MAIN_WIN, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(MAIN_WIN, self._cam.width, self._cam.height)
        cv2.setMouseCallback(MAIN_WIN, self._mouse_cb)

        if self._virtual_cam_enabled:
            self._open_virtual_cam()

        blank = np.zeros((self._cam.height, self._cam.width, 3), dtype=np.uint8)
        delay_ms = max(1, 1000 // self._display_fps)

        try:
            while self._running:
                if self._suite is not None:
                    self._suite.tick()
                    self._suite.flush_hdf5()

                self._flush_hdf5()
                self._pump_tk()

                with self._hdf5_lock:
                    hdf5_rec = self._hdf5_writer is not None

                if self._ts_surface is not None:
                    with self._frame_lock:
                        ts_us = self._latest_ts_us
                        rate  = self._event_rate
                    frame = self._render_time_surface(ts_us)
                else:
                    with self._frame_lock:
                        frame = (self._latest_frame.copy()
                                 if self._latest_frame is not None else blank.copy())
                        ts_us = self._latest_ts_us
                        rate  = self._event_rate

                if self._tracker is not None and self._tracker.is_active:
                    self._tracker.update(frame)

                if self._vcam is not None:
                    # Send the clean render — before the HUD overlay is drawn in-place
                    # below — so badges/timestamps/hints don't show up in a video call.
                    try:
                        self._vcam.send(frame)
                    except Exception as exc:
                        print(f"[WARN] virtual cam send failed, disabling: {exc}", file=sys.stderr)
                        self._vcam.close()
                        self._vcam = None

                display = self._draw_overlay(frame, ts_us, rate, hdf5_rec)
                cv2.imshow(MAIN_WIN, display)

                if cv2.getWindowProperty(MAIN_WIN, cv2.WND_PROP_VISIBLE) < 1:
                    break

                key = cv2.waitKey(delay_ms) & 0xFF
                self._handle_key(key)

        finally:
            self._running = False
            self._stop_hdf5()
            self._cam.stop_raw_recording()
            cv2.destroyAllWindows()
            if self._vcam is not None:
                self._vcam.close()
                self._vcam = None
            if self._tk_root is not None:
                self._close_bias_panel()
            if self._filter_tk_root is not None:
                self._close_filter_panel()
            if self._tk_master is not None:
                try:
                    self._tk_master.destroy()
                except Exception:
                    pass
                self._tk_master = None
            evt_thread.join(timeout=2.0)

    def _render_time_surface(self, current_ts: int) -> np.ndarray:
        # Dark palette colours in BGR (matched from ColorPalette.Dark SDK values)
        _BG  = np.array([52.,  37.,  30.],  dtype=np.float32)  # background
        _ON  = np.array([255., 255., 255.], dtype=np.float32)  # positive events
        _OFF = np.array([200., 126., 64.],  dtype=np.float32)  # negative events

        H, W = self._cam.height, self._cam.width

        active = self._ts_surface >= 0
        if not np.any(active):
            return np.broadcast_to(_BG.astype(np.uint8), (H, W, 3)).copy()

        # Alpha: 1.0 = just happened, 0.0 = older than accum_us
        age = np.where(active, current_ts - self._ts_surface, self._accum_us)
        alpha = np.clip(
            1.0 - age.astype(np.float32) / max(self._accum_us, 1),
            0.0, 1.0,
        )[:, :, np.newaxis]  # (H, W, 1) for broadcasting

        # Per-pixel target colour
        on_mask = (self._pol_surface > 0)[:, :, np.newaxis]
        target = np.where(on_mask, _ON, _OFF)

        # Blend from background toward event colour
        frame = (_BG + alpha * (target - _BG)).clip(0, 255).astype(np.uint8)
        # Inactive pixels (alpha=0) naturally revert to _BG via the blend
        frame[~active] = _BG.astype(np.uint8)

        return frame

    def _apply_center_roi(self) -> None:
        frac = _CENTER_ROI_FRACTIONS[self._center_roi_idx]
        W, H = self._cam.width, self._cam.height
        w, h = int(W * frac), int(H * frac)
        x, y = (W - w) // 2, (H - h) // 2
        self._active_roi = (x, y, x + w, y + h)
        ok = self._cam.set_roi(x, y, w, h)
        status = "applied" if ok else "failed (no hardware ROI support)"
        print(f"Centered ROI {w}×{h} at ({x},{y}) [{int(frac * 100)}%] {status}")

    def _step_speed(self, direction: int) -> None:
        if self._iterator is None or not hasattr(self._iterator, "replay_speed"):
            return
        try:
            idx = _SPEED_STEPS.index(self._replay_speed)
        except ValueError:
            idx = _SPEED_STEPS.index(1.0)
        idx = max(0, min(len(_SPEED_STEPS) - 1, idx + direction))
        self._replay_speed = _SPEED_STEPS[idx]
        self._iterator.replay_speed = self._replay_speed
        label = "∞" if self._replay_speed == 0 else f"×{self._replay_speed:g}"
        print(f"Replay speed: {label}")

    def _handle_key(self, key: int) -> None:
        if key in (27, ord("q"), ord("Q")):
            self._running = False

        elif key in (ord("r"), ord("R")):
            if self._file_mode:
                print("[INFO] RAW recording not available in file playback mode.")
            elif self._cam.is_raw_recording:
                self._cam.stop_raw_recording()
            else:
                path = f"recording_{datetime.now():%Y%m%d_%H%M%S}.raw"
                self._cam.start_raw_recording(path)

        elif key in (ord("h"), ord("H")):
            if self._file_mode:
                print("[INFO] HDF5 recording not available in file playback mode.")
            else:
                with self._hdf5_lock:
                    active = self._hdf5_writer is not None
                if active:
                    self._stop_hdf5()
                else:
                    self._start_hdf5()

        elif key in (ord("b"), ord("B")):
            if self._tk_root is not None:
                self._close_bias_panel()
            else:
                self._open_bias_panel()

        elif key in (ord("f"), ord("F")):
            if self._filter_tk_root is not None:
                self._close_filter_panel()
            else:
                self._open_filter_panel()

        elif key in (ord("c"), ord("C")):
            self._active_roi = None
            self._center_roi_idx = -1
            self._cam.clear_roi()
            print("ROI cleared — full sensor active.")

        elif key in (ord("o"), ord("O")):
            next_idx = self._center_roi_idx + 1
            if next_idx >= len(_CENTER_ROI_FRACTIONS):
                self._center_roi_idx = -1
                self._active_roi = None
                self._cam.clear_roi()
                print("ROI cleared — full sensor active.")
            else:
                self._center_roi_idx = next_idx
                self._apply_center_roi()

        elif key in (ord("+"), ord("=")):
            self._accum_us = min(self._accum_us + _ACCUM_STEP_US, _ACCUM_MAX_US)
            if self._frame_gen is not None:
                self._frame_gen.set_accumulation_time_us(self._accum_us)
            print(f"Accumulation: {self._accum_us // 1000} ms")

        elif key in (ord("-"), ord("_")):
            self._accum_us = max(self._accum_us - _ACCUM_STEP_US, _ACCUM_MIN_US)
            if self._frame_gen is not None:
                self._frame_gen.set_accumulation_time_us(self._accum_us)
            print(f"Accumulation: {self._accum_us // 1000} ms")

        elif key in (ord(","), ord("<")):
            self._step_speed(-1)

        elif key in (ord("."), ord(">")):
            self._step_speed(+1)

        elif key == ord(" "):
            self._paused = not self._paused
            print("Paused." if self._paused else "Resumed.")

        elif key in (ord("k"), ord("K")):
            if self._tracker_selecting:
                self._tracker_selecting = False
                self._tracker_drag_start = None
                self._tracker_drag_rect = None
                print("[TRACK] Selection cancelled.")
            elif self._tracker is not None and self._tracker.is_active:
                self._tracker.reset()
                print("[TRACK] Tracking stopped.")
            else:
                self._tracker_selecting = True
                print("[TRACK] Draw a box around the object to track.")

        elif key in (ord("t"), ord("T")):
            if self._suite is not None:
                self._suite.start_or_stop()
            else:
                print("[INFO] No suite loaded — pass --suite <config.json> to main.py")

        elif key in (ord("n"), ord("N")):
            if self._suite is not None and self._suite.is_active:
                self._suite.skip()
            elif self._suite is not None:
                print("[INFO] Suite is not running.")

        elif key in (ord("s"), ord("S")):
            path = f"snapshot_{datetime.now():%Y%m%d_%H%M%S}.png"
            with self._frame_lock:
                snap = self._latest_frame.copy() if self._latest_frame is not None else None
                ts_us = self._latest_ts_us
                rate  = self._event_rate
            with self._hdf5_lock:
                hdf5_rec = self._hdf5_writer is not None
            if snap is not None:
                cv2.imwrite(path, self._draw_overlay(snap, ts_us, rate, hdf5_rec))
                print(f"Snapshot saved: {path}")

        elif key == ord("]"):
            if self._playlist is not None:
                self._playlist.next_file()

        elif key == ord("["):
            if self._playlist is not None:
                self._playlist.prev_file()
