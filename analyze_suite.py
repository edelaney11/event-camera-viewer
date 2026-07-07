#!/usr/bin/env python3
"""Analyze and compare output from a suite_runner.py bias-sweep suite.

Subcommands:
    compare   Text-table diff of total event counts between two suites
    rates     Plot ON/OFF event rate over time for each step
    sweep     Plot mean ON/OFF event rate vs. the swept bias value
    spatial   Spatial/polarity/temporal comparison between two suites

Usage:
    python analyze_suite.py compare a/suite_metadata.json b/suite_metadata.json [--rate]
    python analyze_suite.py rates suite_metadata.json
    python analyze_suite.py rates a.json b.json --label-a Run1 --label-b Run2 --output plots/
    python analyze_suite.py sweep suite_metadata.json
    python analyze_suite.py sweep a.json b.json --label-a screen_off --label-b screen_on
    python analyze_suite.py spatial a.json b.json [--step bias_diff_-25] [--output plots/]
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import NamedTuple

import h5py
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

_CHUNK = 1_000_000  # events read per HDF5 iteration
_MIN_EVENTS = 1_000  # fewer events than this -> unreliable rate, treat as no data

# Per-suite colour pairs: (ON colour, OFF colour)
_SUITE_COLOURS = [
    ("steelblue", "firebrick"),
    ("darkorange", "seagreen"),
]


# ── Shared suite/metadata helpers ─────────────────────────────────────────────

def load_metadata(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def step_key(step: dict) -> str:
    """Canonical key for matching steps across suites: sorted bias dict."""
    return json.dumps(step["biases"], sort_keys=True)


def suite_label(path: str, label: str | None) -> str:
    return label or Path(path).parent.name


def fmt_events(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def fmt_rate(events: int, duration_s: float) -> str:
    if duration_s <= 0:
        return "—"
    r = events / duration_s
    if r >= 1_000_000:
        return f"{r / 1_000_000:.2f}M/s"
    if r >= 1_000:
        return f"{r / 1_000:.1f}k/s"
    return f"{r:.0f}/s"


# ── Shared HDF5 event-stat loading ────────────────────────────────────────────

class EventStats(NamedTuple):
    total: int
    on_total: int
    off_total: int
    duration_s: float
    rate_t: np.ndarray     # bin centres in seconds
    rate_on: np.ndarray    # ON events/s per bin
    rate_off: np.ndarray   # OFF events/s per bin
    density: np.ndarray | None  # (H, W) total counts, only when spatial=True
    on_map: np.ndarray | None   # (H, W) ON counts, only when spatial=True
    off_map: np.ndarray | None  # (H, W) OFF counts, only when spatial=True


def load_event_stats(path: str, rate_bin_ms: int = 50, spatial: bool = False) -> EventStats:
    """Read an HDF5 recording in chunks, computing ON/OFF rate-over-time and,
    when spatial=True, per-pixel density/polarity maps in a single pass."""
    with h5py.File(path, "r") as f:
        evg = f["events"]
        n = len(evg["t"])

        if n == 0:
            empty_map = None
            if spatial:
                empty_map = np.zeros((int(f.attrs["height"]), int(f.attrs["width"])), dtype=np.int64)
            zeros = np.array([0.0])
            return EventStats(0, 0, 0, 0.0, zeros, zeros, zeros, empty_map, empty_map, empty_map)

        width, height = int(f.attrs["width"]), int(f.attrs["height"])
        t0, t1 = int(evg["t"][0]), int(evg["t"][-1])
        duration_s = (t1 - t0) / 1_000_000

        bin_us = rate_bin_ms * 1_000
        n_bins = max(1, int(np.ceil((t1 - t0) / bin_us)))
        rate_on_c = np.zeros(n_bins, dtype=np.int64)
        rate_off_c = np.zeros(n_bins, dtype=np.int64)

        if spatial:
            density = np.zeros(height * width, dtype=np.int64)
            on_flat = np.zeros(height * width, dtype=np.int64)
            off_flat = np.zeros(height * width, dtype=np.int64)

        total = on_total = 0
        for start in range(0, n, _CHUNK):
            end = min(start + _CHUNK, n)
            p = evg["p"][start:end].astype(np.int32)
            t = evg["t"][start:end]

            bins = np.clip(((t - t0) // bin_us).astype(np.int64), 0, n_bins - 1)
            rate_on_c += np.bincount(bins[p == 1], minlength=n_bins)
            rate_off_c += np.bincount(bins[p == 0], minlength=n_bins)
            total += len(p)
            on_total += int((p == 1).sum())

            if spatial:
                x = evg["x"][start:end].astype(np.int32)
                y = evg["y"][start:end].astype(np.int32)
                valid = (x >= 0) & (x < width) & (y >= 0) & (y < height)
                idx = y[valid] * width + x[valid]
                pv = p[valid]
                density += np.bincount(idx, minlength=height * width)
                on_flat += np.bincount(idx[pv == 1], minlength=height * width)
                off_flat += np.bincount(idx[pv == 0], minlength=height * width)

        bin_s = rate_bin_ms / 1000
        rate_t = (np.arange(n_bins) + 0.5) * bin_s
        rate_on = rate_on_c / bin_s
        rate_off = rate_off_c / bin_s

        return EventStats(
            total=total, on_total=on_total, off_total=total - on_total, duration_s=duration_s,
            rate_t=rate_t, rate_on=rate_on, rate_off=rate_off,
            density=density.reshape(height, width) if spatial else None,
            on_map=on_flat.reshape(height, width) if spatial else None,
            off_map=off_flat.reshape(height, width) if spatial else None,
        )


# ── compare ────────────────────────────────────────────────────────────────

def cmd_compare(args: argparse.Namespace) -> None:
    data_a = load_metadata(args.metadata_a)
    data_b = load_metadata(args.metadata_b)
    label_a = suite_label(args.metadata_a, args.label_a)
    label_b = suite_label(args.metadata_b, args.label_b)

    index_a: dict[str, dict] = {step_key(s): s for s in data_a["steps"]}
    index_b: dict[str, dict] = {step_key(s): s for s in data_b["steps"]}
    all_keys = list(dict.fromkeys(list(index_a) + list(index_b)))  # preserve order

    col_name = max(len("Setting"), max(len(s.get("name", "?")) for s in data_a["steps"] + data_b["steps"]))
    col_val = max(len(label_a), len(label_b), 10)
    col_delta = 12
    header_val = "ev/s" if args.rate else "events"

    print(f"\nSuite A: {args.metadata_a}  [{label_a}]")
    print(f"Suite B: {args.metadata_b}  [{label_b}]")
    print()

    sep = "-" * (col_name + 2 + col_val + 2 + col_val + 2 + col_delta + 2 + 8)
    print(sep)
    print(
        f"{'Setting':<{col_name}}  "
        f"{label_a + ' ' + header_val:>{col_val}}  "
        f"{label_b + ' ' + header_val:>{col_val}}  "
        f"{'delta':>{col_delta}}  "
        f"{'change':>8}"
    )
    print(sep)

    only_a = only_b = matched = 0

    for key in all_keys:
        sa = index_a.get(key)
        sb = index_b.get(key)
        name = (sa or sb)["name"]

        if sa is None:
            val_a_str = "—"
            val_b_str = (fmt_rate(sb["total_events"], sb["actual_duration_s"])
                         if args.rate else fmt_events(sb["total_events"]))
            delta_str = change_str = "only B"
            only_b += 1
        elif sb is None:
            val_a_str = (fmt_rate(sa["total_events"], sa["actual_duration_s"])
                         if args.rate else fmt_events(sa["total_events"]))
            val_b_str = "—"
            delta_str = change_str = "only A"
            only_a += 1
        else:
            matched += 1
            if args.rate:
                va = sa["total_events"] / max(sa["actual_duration_s"], 1e-9)
                vb = sb["total_events"] / max(sb["actual_duration_s"], 1e-9)
                val_a_str = fmt_rate(sa["total_events"], sa["actual_duration_s"])
                val_b_str = fmt_rate(sb["total_events"], sb["actual_duration_s"])
                delta = vb - va
                delta_str = f"{'+' if delta >= 0 else ''}{delta / 1_000_000:.3f}M/s" if abs(delta) >= 1e6 else f"{'+' if delta >= 0 else ''}{delta / 1_000:.1f}k/s"
            else:
                va = sa["total_events"]
                vb = sb["total_events"]
                val_a_str = fmt_events(va)
                val_b_str = fmt_events(vb)
                delta = vb - va
                delta_str = ("+" if delta >= 0 else "-") + fmt_events(abs(delta))

            if va > 0:
                pct = (vb - va) / va * 100
                change_str = f"{'+' if pct >= 0 else ''}{pct:.1f}%"
            else:
                change_str = "—"

        print(
            f"{name:<{col_name}}  "
            f"{val_a_str:>{col_val}}  "
            f"{val_b_str:>{col_val}}  "
            f"{delta_str:>{col_delta}}  "
            f"{change_str:>8}"
        )

    print(sep)
    print(f"  {matched} matched  |  {only_a} only in A  |  {only_b} only in B")

    total_a = data_a.get("total_events")
    total_b = data_b.get("total_events")
    if total_a is not None and total_b is not None:
        print(f"\n  Total: {label_a} {fmt_events(total_a)}  |  {label_b} {fmt_events(total_b)}"
              f"  |  delta {('+' if total_b >= total_a else '')}{fmt_events(abs(total_b - total_a))}")
    print()


# ── rates ──────────────────────────────────────────────────────────────────

def _plot_rates_on_axes(ax, step: dict, suite_dir: Path, label: str,
                         colour_on: str, colour_off: str, rate_bin_ms: int) -> tuple[int, int] | None:
    path = suite_dir / step["output_files"][0]
    if not path.exists():
        return None

    stats = load_event_stats(str(path), rate_bin_ms)
    if stats.total == 0:
        return (0, 0)

    scale = 1e6
    ax.plot(stats.rate_t, stats.rate_on / scale,
            color=colour_on, linewidth=0.9, linestyle="-", label=f"{label} ON")
    ax.plot(stats.rate_t, -stats.rate_off / scale,
            color=colour_off, linewidth=0.9, linestyle="-", label=f"{label} OFF")
    return stats.total, stats.on_total


def cmd_rates(args: argparse.Namespace) -> None:
    data_a = load_metadata(args.metadata_a)
    dir_a = Path(args.metadata_a).parent
    label_a = suite_label(args.metadata_a, args.label_a)
    index_a = {step_key(s): s for s in data_a["steps"]}

    comparing = args.metadata_b is not None
    if comparing:
        data_b = load_metadata(args.metadata_b)
        dir_b = Path(args.metadata_b).parent
        label_b = suite_label(args.metadata_b, args.label_b)
        index_b = {step_key(s): s for s in data_b["steps"]}
        all_keys = list(dict.fromkeys(list(index_a) + list(index_b)))
    else:
        all_keys = [step_key(s) for s in data_a["steps"]]

    n_steps = len(all_keys)
    ncols = min(3, n_steps)
    nrows = math.ceil(n_steps / ncols)

    fig, axes = plt.subplots(nrows, ncols, figsize=(6 * ncols, 3.5 * nrows), squeeze=False)
    title = (f"Event rate per bias setting — {label_a} vs {label_b}" if comparing
             else f"Event rate per bias setting — {label_a}")
    fig.suptitle(title, fontsize=13, fontweight="bold")

    col_a_on, col_a_off = _SUITE_COLOURS[0]
    col_b_on, col_b_off = _SUITE_COLOURS[1]

    for i, key in enumerate(all_keys):
        ax = axes[i // ncols][i % ncols]
        step_a = index_a.get(key)
        step_b = index_b.get(key) if comparing else None

        ref_step = step_a or step_b
        name = ref_step["name"]
        bias_str = "  ".join(f"{k}={v}" for k, v in ref_step["biases"].items()) or "default"

        print(f"Loading '{name}' …", flush=True)
        title_lines = [name, bias_str]
        any_data = False

        if step_a is not None:
            result = _plot_rates_on_axes(ax, step_a, dir_a, label_a if comparing else "ON / OFF",
                                          col_a_on, col_a_off, args.rate_bin_ms)
            if result is None:
                title_lines.append(f"{label_a}: file not found")
            elif result[0] == 0:
                title_lines.append(f"{label_a}: no events")
            else:
                total, on_total = result
                title_lines.append(f"{label_a}: {total:,} ev  {100 * on_total / total:.1f}% ON")
                any_data = True
        else:
            title_lines.append(f"{label_a}: —")

        if comparing and step_b is not None:
            result = _plot_rates_on_axes(ax, step_b, dir_b, label_b, col_b_on, col_b_off, args.rate_bin_ms)
            if result is None:
                title_lines.append(f"{label_b}: file not found")
            elif result[0] == 0:
                title_lines.append(f"{label_b}: no events")
            else:
                total, on_total = result
                title_lines.append(f"{label_b}: {total:,} ev  {100 * on_total / total:.1f}% ON")
                any_data = True
        elif comparing:
            title_lines.append(f"{label_b}: —")

        if not any_data:
            ax.text(0.5, 0.5, "no data", transform=ax.transAxes, ha="center", va="center", color="grey")
        else:
            ax.axhline(0, color="black", linewidth=0.5, alpha=0.35)
            ax.set_xlabel("Time (s)", fontsize=8)
            ax.set_ylabel("Mev/s   (ON ↑  OFF ↓)", fontsize=8)
            ax.legend(fontsize=7, loc="upper right")
            ax.grid(True, alpha=0.3)

        ax.set_title("\n".join(title_lines), fontsize=8)
        ax.tick_params(labelsize=7)

    for j in range(n_steps, nrows * ncols):
        axes[j // ncols][j % ncols].set_visible(False)

    fig.tight_layout()
    _show_or_save(fig, args.output, "suite_rates.png")


# ── sweep ──────────────────────────────────────────────────────────────────

def infer_x_axis(steps: list[dict]) -> tuple[str, list[float]]:
    """Return (axis_label, x_values) by finding which bias parameters vary."""
    all_keys: list[str] = []
    seen: set[str] = set()
    for s in steps:
        for k in s["biases"]:
            if k not in seen:
                all_keys.append(k)
                seen.add(k)

    varying = {
        k: [s["biases"].get(k, 0) for s in steps]
        for k in all_keys
        if len({s["biases"].get(k, 0) for s in steps}) > 1
    }

    if not varying:
        return "step", list(range(len(steps)))

    first_vals = next(iter(varying.values()))
    if all(v == first_vals for v in varying.values()):
        return " = ".join(varying.keys()), [float(v) for v in first_vals]

    first_key = next(iter(varying))
    return first_key, [float(v) for v in varying[first_key]]


def collect_avg_rates(steps: list[dict], suite_dir: Path) -> tuple[np.ndarray, np.ndarray]:
    """Return (on_rates, off_rates) in ev/s, one value per step (nan if unreliable)."""
    on_rates = np.full(len(steps), np.nan)
    off_rates = np.full(len(steps), np.nan)
    for i, step in enumerate(steps):
        path = suite_dir / step["output_files"][0]
        if not path.exists():
            print(f"  Warning: {path} not found, skipping.")
            continue
        stats = load_event_stats(str(path))
        if stats.total < _MIN_EVENTS or stats.duration_s <= 0:
            continue
        on_rates[i] = stats.on_total / stats.duration_s
        off_rates[i] = stats.off_total / stats.duration_s
    return on_rates, off_rates


def cmd_sweep(args: argparse.Namespace) -> None:
    data_a = load_metadata(args.metadata_a)
    dir_a = Path(args.metadata_a).parent
    label_a = suite_label(args.metadata_a, args.label_a)
    steps_a = data_a["steps"]

    comparing = args.metadata_b is not None
    if comparing:
        data_b = load_metadata(args.metadata_b)
        dir_b = Path(args.metadata_b).parent
        label_b = suite_label(args.metadata_b, args.label_b)
        steps_b = data_b["steps"]
        index_b = {step_key(s): s for s in steps_b}

        # Align suite B to suite A's step order; carry over unmatched B steps at end
        aligned_b: list[dict | None] = []
        seen_keys: set[str] = set()
        for s in steps_a:
            k = step_key(s)
            aligned_b.append(index_b.get(k))
            seen_keys.add(k)
        for s in steps_b:
            if step_key(s) not in seen_keys:
                steps_a.append(s)  # extend A list with unmatched B steps
                aligned_b.append(s)
                seen_keys.add(step_key(s))

    x_label, x_vals = infer_x_axis(steps_a)
    x = np.array(x_vals, dtype=float)

    print(f"Loading {label_a} …")
    on_a, off_a = collect_avg_rates(steps_a, dir_a)

    if comparing:
        dummy = {"output_files": ["__missing__"], "biases": {}}
        steps_b_real = [s if s is not None else dummy for s in aligned_b]
        print(f"Loading {label_b} …")
        on_b, off_b = collect_avg_rates(steps_b_real, dir_b)

    fig, ax = plt.subplots(figsize=(10, 5))
    title = (f"Average event rate vs bias — {label_a} vs {label_b}" if comparing
             else f"Average event rate vs bias — {label_a}")
    ax.set_title(title, fontsize=12, fontweight="bold")

    scale = 1e6
    mk = dict(markersize=4, linewidth=1.2)

    if comparing:
        ax.plot(x, on_a / scale, "o-", color="steelblue", label=f"{label_a} ON", **mk)
        ax.plot(x, off_a / scale, "s--", color="steelblue", label=f"{label_a} OFF", **mk)
        ax.plot(x, on_b / scale, "o-", color="darkorange", label=f"{label_b} ON", **mk)
        ax.plot(x, off_b / scale, "s--", color="darkorange", label=f"{label_b} OFF", **mk)
    else:
        ax.plot(x, on_a / scale, "o-", color="steelblue", label="ON", **mk)
        ax.plot(x, off_a / scale, "s--", color="firebrick", label="OFF", **mk)

    ax.set_xlabel(x_label, fontsize=10)
    ax.set_ylabel("Mean event rate (Mev/s)", fontsize=10)
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    _show_or_save(fig, args.output, "suite_sweep.png")


# ── spatial ────────────────────────────────────────────────────────────────

def plot_spatial_step(name: str, biases: dict, sa: EventStats, sb: EventStats,
                       label_a: str, label_b: str) -> plt.Figure:
    fig = plt.figure(figsize=(14, 12))
    bias_str = "  ".join(f"{k}={v}" for k, v in biases.items())
    fig.suptitle(f"{name}     [{bias_str}]", fontsize=12, fontweight="bold")

    gs = gridspec.GridSpec(3, 2, figure=fig, hspace=0.5, wspace=0.35)

    # Row 0: event density (log scale)
    vmax = np.log1p(max(sa.density.max(), sb.density.max()))
    for col, (stats, label) in enumerate([(sa, label_a), (sb, label_b)]):
        ax = fig.add_subplot(gs[0, col])
        im = ax.imshow(np.log1p(stats.density), origin="upper", cmap="inferno",
                        vmin=0, vmax=vmax, aspect="equal")
        plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label="log(1 + count)")
        on_pct = 100 * stats.on_total / stats.total if stats.total else 0
        ax.set_title(f"{label}\nDensity  |  {stats.total:,} events  |  {on_pct:.1f}% ON", fontsize=9)
        ax.set_xlabel("x"); ax.set_ylabel("y")

    # Row 1: polarity balance per pixel
    for col, (stats, label) in enumerate([(sa, label_a), (sb, label_b)]):
        ax = fig.add_subplot(gs[1, col])
        total_px = stats.on_map + stats.off_map
        with np.errstate(invalid="ignore", divide="ignore"):
            frac_on = np.where(total_px > 0, stats.on_map / total_px.astype(float), np.nan)
        im = ax.imshow(frac_on, origin="upper", cmap="RdBu_r", vmin=0, vmax=1, aspect="equal")
        cb = plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        cb.set_label("fraction ON  (0=OFF, 1=ON)")
        ratio = (stats.on_total / stats.off_total) if stats.off_total else float("inf")
        ax.set_title(f"{label}\nPolarity  |  ON/OFF ratio {ratio:.3f}", fontsize=9)
        ax.set_xlabel("x"); ax.set_ylabel("y")

    # Row 2: event rate over time (ON positive, OFF negative)
    ax_rate = fig.add_subplot(gs[2, :])
    for stats, label, col in [(sa, label_a, "steelblue"), (sb, label_b, "darkorange")]:
        ax_rate.plot(stats.rate_t, stats.rate_on / 1e6, color=col, linestyle="-",
                     linewidth=0.8, alpha=0.9, label=f"{label} ON")
        ax_rate.plot(stats.rate_t, -stats.rate_off / 1e6, color=col, linestyle="--",
                     linewidth=0.8, alpha=0.9, label=f"{label} OFF")

    ax_rate.axhline(0, color="white", linewidth=0.6, alpha=0.4)
    ax_rate.set_xlabel("Time (s)")
    ax_rate.set_ylabel("Event rate (Mev/s)  —  ON ↑   OFF ↓")
    ax_rate.set_title("Event rate over time")
    ax_rate.legend(fontsize=8, ncol=2)
    ax_rate.grid(True, alpha=0.3)

    return fig


def cmd_spatial(args: argparse.Namespace) -> None:
    data_a = load_metadata(args.metadata_a)
    data_b = load_metadata(args.metadata_b)
    dir_a = Path(args.metadata_a).parent
    dir_b = Path(args.metadata_b).parent
    label_a = suite_label(args.metadata_a, args.label_a)
    label_b = suite_label(args.metadata_b, args.label_b)

    index_a = {step_key(s): s for s in data_a["steps"]}
    index_b = {step_key(s): s for s in data_b["steps"]}
    all_keys = list(dict.fromkeys(list(index_a) + list(index_b)))

    out_dir = None
    if args.output:
        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)

    for key in all_keys:
        step_a = index_a.get(key)
        step_b = index_b.get(key)

        if step_a is None or step_b is None:
            missing = (step_a or step_b)["name"]
            print(f"Skipping '{missing}' — only present in one suite.")
            continue

        name = step_a["name"]
        if args.step and name != args.step:
            continue

        path_a = dir_a / step_a["output_files"][0]
        path_b = dir_b / step_b["output_files"][0]

        for path in (path_a, path_b):
            if not path.exists():
                print(f"Skipping '{name}' — {path} not found.")
                break
        else:
            print(f"Processing '{name}' …", flush=True)
            stats_a = load_event_stats(str(path_a), args.rate_bin_ms, spatial=True)
            stats_b = load_event_stats(str(path_b), args.rate_bin_ms, spatial=True)

            if stats_a.total == 0 and stats_b.total == 0:
                print(f"  Skipping '{name}' — both files are empty.")
                continue
            if stats_a.total == 0:
                print(f"  Warning: '{name}' — {label_a} file is empty, plot may be blank.")
            if stats_b.total == 0:
                print(f"  Warning: '{name}' — {label_b} file is empty, plot may be blank.")

            fig = plot_spatial_step(name, step_a["biases"], stats_a, stats_b, label_a, label_b)

            if out_dir:
                out_path = out_dir / f"{name}.png"
                fig.savefig(out_path, dpi=150, bbox_inches="tight")
                print(f"  Saved {out_path}")
                plt.close(fig)
            else:
                plt.show()


# ── Output helper + CLI ───────────────────────────────────────────────────────

def _show_or_save(fig: plt.Figure, output: str | None, filename: str) -> None:
    if output:
        out_dir = Path(output)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / filename
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
        print(f"Saved {out_path}")
    else:
        plt.show()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Analyze and compare bias-sweep suite output.")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("compare", help="Text-table diff of total event counts between two suites.")
    sp.add_argument("metadata_a", help="First suite_metadata.json")
    sp.add_argument("metadata_b", help="Second suite_metadata.json")
    sp.add_argument("--label-a", default=None, metavar="LABEL", help="Label for the first suite (default: filename)")
    sp.add_argument("--label-b", default=None, metavar="LABEL", help="Label for the second suite (default: filename)")
    sp.add_argument("--rate", action="store_true", help="Show events/s instead of raw counts")

    sp = sub.add_parser("rates", help="Plot ON/OFF event rate over time for each step.")
    sp.add_argument("metadata_a", help="suite_metadata.json (first/only suite)")
    sp.add_argument("metadata_b", nargs="?", default=None, help="Second suite_metadata.json — overlay for comparison")
    sp.add_argument("--label-a", default=None, metavar="LABEL")
    sp.add_argument("--label-b", default=None, metavar="LABEL")
    sp.add_argument("--output", default=None, metavar="DIR", help="Save suite_rates.png here instead of showing interactively")
    sp.add_argument("--rate-bin-ms", type=int, default=50, metavar="MS", help="Time bin width in ms (default: 50)")

    sp = sub.add_parser("sweep", help="Plot mean ON/OFF event rate vs. the swept bias value.")
    sp.add_argument("metadata_a", help="suite_metadata.json (first/only suite)")
    sp.add_argument("metadata_b", nargs="?", default=None, help="Second suite_metadata.json for comparison")
    sp.add_argument("--label-a", default=None, metavar="LABEL")
    sp.add_argument("--label-b", default=None, metavar="LABEL")
    sp.add_argument("--output", default=None, metavar="DIR", help="Save suite_sweep.png here instead of showing interactively")

    sp = sub.add_parser("spatial", help="Spatial, polarity, and temporal comparison between two suite runs.")
    sp.add_argument("metadata_a", help="First suite_metadata.json")
    sp.add_argument("metadata_b", help="Second suite_metadata.json")
    sp.add_argument("--label-a", default=None, metavar="LABEL")
    sp.add_argument("--label-b", default=None, metavar="LABEL")
    sp.add_argument("--step", default=None, metavar="NAME", help="Only plot this setting name (default: all matched steps)")
    sp.add_argument("--output", default=None, metavar="DIR", help="Save PNGs here instead of showing interactively")
    sp.add_argument("--rate-bin-ms", type=int, default=50, metavar="MS", help="Time bin width for event-rate plot (default: 50 ms)")

    return p


def main() -> None:
    args = build_parser().parse_args()
    {
        "compare": cmd_compare,
        "rates": cmd_rates,
        "sweep": cmd_sweep,
        "spatial": cmd_spatial,
    }[args.command](args)


if __name__ == "__main__":
    main()
