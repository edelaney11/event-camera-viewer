"""HAL device wrapper: biases, ROI, RAW recording, and noise/rate filters."""
from __future__ import annotations

import sys
from typing import NamedTuple

import metavision_hal as mv_hal
from metavision_core.event_io.raw_reader import initiate_device
from metavision_core.event_io import EventsIterator

# Standard Prophesee bias names in display order
STANDARD_BIASES = [
    "bias_diff",
    "bias_diff_on",
    "bias_diff_off",
    "bias_fo",
    "bias_hpf",
    "bias_refr",
    "bias_pr",
]

# Recommended (not the wider hardware-"allowed") bias ranges per sensor
# generation, from Prophesee's Biases documentation:
# https://docs.prophesee.ai/stable/hw/manuals/biases.html
# IMX636 values are offsets from factory default; GenX320 values are absolute.
RECOMMENDED_BIAS_RANGES: dict[str, dict[str, tuple[int, int]]] = {
    "IMX636": {
        "bias_diff":     (-25, 23),
        "bias_diff_on":  (-85, 140),
        "bias_diff_off": (-35, 190),
        "bias_fo":       (-35, 55),
        "bias_hpf":      (0, 120),
        "bias_refr":     (-20, 235),
    },
    "GENX320": {
        "bias_diff":     (41, 51),
        "bias_diff_on":  (24, 60),
        "bias_diff_off": (19, 50),
        "bias_fo":       (19, 39),
        "bias_hpf":      (0, 127),
        "bias_refr":     (0, 127),
    },
}


class BiasInfo(NamedTuple):
    name: str
    value: int
    min_val: int
    max_val: int
    description: str


def _bias_range(info) -> tuple[int, int]:
    """Extract (min, max) from a LL_Bias_Info object, trying multiple accessors."""
    for method in ("get_bias_allowed_range", "get_bias_range", "get_bias_recommended_range"):
        fn = getattr(info, method, None)
        if not callable(fn):
            continue
        try:
            rng = fn()
            if hasattr(rng, "__len__") and len(rng) == 2:
                return int(rng[0]), int(rng[1])
            if hasattr(rng, "min_val") and hasattr(rng, "max_val"):
                return int(rng.min_val), int(rng.max_val)
        except Exception:
            pass
    return 0, 1800


def _detect_sensor_key(device) -> str | None:
    """Identify the sensor generation so the matching recommended bias ranges
    can be applied. Returns a key into RECOMMENDED_BIAS_RANGES, or None if the
    sensor isn't recognised (in which case no extra clamping is applied)."""
    try:
        hw_id = device.get_i_hw_identification()
        if hw_id is None:
            return None
        name = hw_id.get_sensor_info().name.upper()
    except Exception:
        return None
    if "IMX636" in name:
        return "IMX636"
    if "GENX320" in name or "GEN X320" in name:
        return "GENX320"
    return None


class CameraManager:
    def __init__(self) -> None:
        self.device = None
        self.width: int = 0
        self.height: int = 0
        self._i_ll_biases = None
        self._i_roi = None
        self._i_events_stream = None
        self._i_antiflicker = None
        self._i_erc = None
        self._i_trail_filter = None
        self._i_event_rate_filter = None
        self._recommended_ranges: dict[str, tuple[int, int]] = {}
        self._raw_recording: bool = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def open(self, serial: str = "") -> None:
        self.device = initiate_device(serial)
        geo = self.device.get_i_geometry()
        self.width = geo.get_width()
        self.height = geo.get_height()
        self._i_ll_biases = self.device.get_i_ll_biases()
        self._i_roi = self.device.get_i_roi()
        self._i_events_stream = self.device.get_i_events_stream()
        # Noise/rate filters — not every sensor or plugin exposes all of these,
        # so each may legitimately be None.
        self._i_antiflicker = self.device.get_i_antiflicker_module()
        self._i_erc = self.device.get_i_erc_module()
        self._i_trail_filter = self.device.get_i_event_trail_filter_module()
        self._i_event_rate_filter = self.device.get_i_event_rate()

        sensor_key = _detect_sensor_key(self.device)
        self._recommended_ranges = RECOMMENDED_BIAS_RANGES.get(sensor_key, {})

    def get_iterator(self, delta_t_us: int = 10000) -> EventsIterator:
        return EventsIterator.from_device(device=self.device, delta_t=delta_t_us)

    def close(self) -> None:
        self.stop_raw_recording()

    def _facility(self, attr_name: str, getter_name: str):
        """Return a cached device facility, retrying the HAL lookup if we don't
        have it yet. Some plugins don't finish enumerating every facility (ERC
        in particular) in the instant after open() returns, so a None result
        the first time isn't necessarily final — we retry lazily on each call
        until it's found, rather than giving up forever after a single check."""
        cached = getattr(self, attr_name)
        if cached is not None or self.device is None:
            return cached
        try:
            fresh = getattr(self.device, getter_name)()
        except Exception:
            fresh = None
        setattr(self, attr_name, fresh)
        return fresh

    def active_filter_tags(self) -> list[str]:
        """Short tags for whichever noise/rate filters are currently enabled —
        cheap enough to call every display frame (a single is_enabled() each),
        unlike get_*_settings() which pulls the full bounds/state dict."""
        tags = []
        for attr_name, getter_name, tag in (
            ("_i_antiflicker", "get_i_antiflicker_module", "AFK"),
            ("_i_erc", "get_i_erc_module", "ERC"),
            ("_i_trail_filter", "get_i_event_trail_filter_module", "TRAIL"),
            ("_i_event_rate_filter", "get_i_event_rate", "RATE"),
        ):
            facility = self._facility(attr_name, getter_name)
            if facility is None:
                continue
            try:
                if facility.is_enabled():
                    tags.append(tag)
            except Exception:
                pass
        return tags

    # ── Bias control ──────────────────────────────────────────────────────────

    def get_all_bias_info(self) -> list[BiasInfo]:
        if self._i_ll_biases is None:
            return []
        try:
            all_biases: dict = self._i_ll_biases.get_all_biases()
        except Exception:
            return []

        result: list[BiasInfo] = []
        # Standard biases first (in defined order)
        for name in STANDARD_BIASES:
            if name not in all_biases:
                continue
            value = int(all_biases[name])
            min_val, max_val = 0, 1800
            description = ""
            try:
                info = self._i_ll_biases.get_bias_info(name)
                if info is not None:
                    min_val, max_val = _bias_range(info)
                    try:
                        description = info.get_description()
                    except Exception:
                        pass
            except Exception:
                pass

            recommended = self._recommended_ranges.get(name)
            if recommended is not None:
                rec_lo, rec_hi = recommended
                min_val, max_val = max(min_val, rec_lo), min(max_val, rec_hi)

            result.append(BiasInfo(name, value, min_val, max_val, description))

        # Any non-standard biases the camera exposes
        for name, raw_val in all_biases.items():
            if name not in STANDARD_BIASES:
                result.append(BiasInfo(name, int(raw_val), 0, 1800, ""))

        return result

    def set_bias(self, name: str, value: int) -> bool:
        if self._i_ll_biases is None:
            return False
        try:
            self._i_ll_biases.set(name, int(value))
            return True
        except Exception as exc:
            print(f"[WARN] set_bias({name}={value}): {exc}", file=sys.stderr)
            return False

    def get_bias(self, name: str) -> int | None:
        if self._i_ll_biases is None:
            return None
        try:
            return int(self._i_ll_biases.get(name))
        except Exception:
            return None

    # ── ROI control ───────────────────────────────────────────────────────────

    def set_roi(self, x: int, y: int, w: int, h: int) -> bool:
        """Set a hardware ROI window.  x,y are the top-left corner; w,h the size."""
        if self._i_roi is None:
            return False
        try:
            win = mv_hal.I_ROI.Window(x, y, w, h)
            self._i_roi.set_window(win)
            self._i_roi.set_mode(mv_hal.I_ROI.Mode.ROI)
            self._i_roi.enable(True)
            return True
        except Exception as exc:
            print(f"[WARN] set_roi({x},{y},{w},{h}): {exc}", file=sys.stderr)
            return False

    def clear_roi(self) -> None:
        if self._i_roi is None:
            return
        try:
            self._i_roi.enable(False)
        except Exception as exc:
            print(f"[WARN] clear_roi: {exc}", file=sys.stderr)

    # ── Anti-flicker filter ───────────────────────────────────────────────────

    def has_antiflicker(self) -> bool:
        return self._facility("_i_antiflicker", "get_i_antiflicker_module") is not None

    def get_antiflicker_settings(self) -> dict | None:
        """Current state and supported bounds, or None if unsupported."""
        f = self._facility("_i_antiflicker", "get_i_antiflicker_module")
        if f is None:
            return None
        try:
            min_freq, max_freq = f.get_frequency_band()
            return {
                "enabled": f.is_enabled(),
                "min_freq": min_freq,
                "max_freq": max_freq,
                "duty_cycle": f.get_duty_cycle(),
                "freq_bounds": (f.get_min_supported_frequency(), f.get_max_supported_frequency()),
                "duty_cycle_bounds": (f.get_min_supported_duty_cycle(), f.get_max_supported_duty_cycle()),
            }
        except Exception:
            return None

    def set_antiflicker(
        self, enabled: bool, min_freq: int | None = None, max_freq: int | None = None,
        duty_cycle: float | None = None,
    ) -> bool:
        f = self._facility("_i_antiflicker", "get_i_antiflicker_module")
        if f is None:
            return False
        try:
            if min_freq is not None and max_freq is not None:
                f.set_frequency_band(int(min_freq), int(max_freq))
            if duty_cycle is not None:
                f.set_duty_cycle(float(duty_cycle))
            f.enable(bool(enabled))
            return True
        except Exception as exc:
            print(f"[WARN] set_antiflicker: {exc}", file=sys.stderr)
            return False

    # ── Event Rate Controller (ERC) ───────────────────────────────────────────

    def has_erc(self) -> bool:
        return self._facility("_i_erc", "get_i_erc_module") is not None

    def get_erc_settings(self) -> dict | None:
        e = self._facility("_i_erc", "get_i_erc_module")
        if e is None:
            return None
        try:
            return {
                "enabled": e.is_enabled(),
                "rate_events_per_sec": e.get_cd_event_rate(),
                "rate_bounds": (e.get_min_supported_cd_event_rate(), e.get_max_supported_cd_event_rate()),
            }
        except Exception:
            return None

    def set_erc(self, enabled: bool, rate_events_per_sec: int | None = None) -> bool:
        e = self._facility("_i_erc", "get_i_erc_module")
        if e is None:
            return False
        try:
            if rate_events_per_sec is not None:
                e.set_cd_event_rate(int(rate_events_per_sec))
            e.enable(bool(enabled))
            return True
        except Exception as exc:
            print(f"[WARN] set_erc: {exc}", file=sys.stderr)
            return False

    # ── Event trail filter ────────────────────────────────────────────────────

    def has_trail_filter(self) -> bool:
        return self._facility("_i_trail_filter", "get_i_event_trail_filter_module") is not None

    def get_trail_filter_settings(self) -> dict | None:
        t = self._facility("_i_trail_filter", "get_i_event_trail_filter_module")
        if t is None:
            return None
        try:
            return {
                "enabled": t.is_enabled(),
                "threshold": t.get_threshold(),
                "threshold_bounds": (t.get_min_supported_threshold(), t.get_max_supported_threshold()),
            }
        except Exception:
            return None

    def set_trail_filter(self, enabled: bool, threshold: int | None = None) -> bool:
        t = self._facility("_i_trail_filter", "get_i_event_trail_filter_module")
        if t is None:
            return False
        try:
            if threshold is not None:
                t.set_threshold(int(threshold))
            t.enable(bool(enabled))
            return True
        except Exception as exc:
            print(f"[WARN] set_trail_filter: {exc}", file=sys.stderr)
            return False

    # ── Event rate activity filter ────────────────────────────────────────────

    def has_event_rate_filter(self) -> bool:
        return self._facility("_i_event_rate_filter", "get_i_event_rate") is not None

    def get_event_rate_filter_settings(self) -> dict | None:
        """Current hysteresis thresholds (events/s) and supported bounds."""
        r = self._facility("_i_event_rate_filter", "get_i_event_rate")
        if r is None:
            return None
        try:
            th = r.get_thresholds()
            min_th = r.get_min_supported_thresholds()
            max_th = r.get_max_supported_thresholds()
            fields = ("lower_bound_start", "lower_bound_stop", "upper_bound_start", "upper_bound_stop")
            return {
                "enabled": r.is_enabled(),
                **{f: getattr(th, f) for f in fields},
                "bounds": {f: (getattr(min_th, f), getattr(max_th, f)) for f in fields},
            }
        except Exception:
            return None

    def set_event_rate_filter(
        self, enabled: bool,
        lower_bound_start: int | None = None, lower_bound_stop: int | None = None,
        upper_bound_start: int | None = None, upper_bound_stop: int | None = None,
    ) -> bool:
        r = self._facility("_i_event_rate_filter", "get_i_event_rate")
        if r is None:
            return False
        try:
            updates = {
                "lower_bound_start": lower_bound_start, "lower_bound_stop": lower_bound_stop,
                "upper_bound_start": upper_bound_start, "upper_bound_stop": upper_bound_stop,
            }
            if any(v is not None for v in updates.values()):
                th = r.get_thresholds()
                for name, value in updates.items():
                    if value is not None:
                        setattr(th, name, int(value))
                r.set_thresholds(th)
            r.enable(bool(enabled))
            return True
        except Exception as exc:
            print(f"[WARN] set_event_rate_filter: {exc}", file=sys.stderr)
            return False

    # ── RAW recording ─────────────────────────────────────────────────────────

    def start_raw_recording(self, path: str) -> bool:
        if self._i_events_stream is None:
            return False
        try:
            ok = self._i_events_stream.log_raw_data(path)
            if ok:
                self._raw_recording = True
                print(f"RAW recording started: {path}")
            return bool(ok)
        except Exception as exc:
            print(f"[WARN] start_raw_recording: {exc}", file=sys.stderr)
            return False

    def stop_raw_recording(self) -> None:
        if not self._raw_recording or self._i_events_stream is None:
            return
        try:
            self._i_events_stream.stop_log_raw_data()
        except Exception as exc:
            print(f"[WARN] stop_raw_recording: {exc}", file=sys.stderr)
        finally:
            self._raw_recording = False
            print("RAW recording stopped.")

    @property
    def is_raw_recording(self) -> bool:
        return self._raw_recording
