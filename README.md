# event-camera-viewer

A live viewer and recorder for Prophesee event cameras, built on [OpenEB](https://github.com/prophesee-ai/openeb). It gives you a real-time display of event data with bias and ROI control, HDF5/RAW recording and playback, automated bias-sweep test suites, object tracking, and tools for exporting and comparing recordings.

Developed and tested against a Prophesee EVK4HD (IMX636 sensor). `camera_manager.py` is sensor-agnostic — resolution, available biases, and ROI support are all queried live from the HAL rather than hardcoded — and the same wrapper was also exercised against a GenX320 sensor during development, so it should work with any camera supported by OpenEB's `metavision_hal`.

## Features

- **Live view** with a modern translucent HUD (status bar, badges, progress bars) and an adjustable event-accumulation window and display FPS
- **Bias control** — read/adjust all standard sensor biases (`bias_diff`, `bias_diff_on/off`, `bias_fo`, `bias_hpf`, `bias_refr`, `bias_pr`) through a dark-themed panel with a slider *and* a type-in field for every value
- **ROI control** — draw a custom region of interest with the mouse, or cycle through preset center-crop ROIs; enforced in hardware
- **Recording** — RAW (native Prophesee format) or a custom chunked HDF5 format, toggled live
- **Playback** — replay a single HDF5/RAW file or a whole folder as a playlist, at any speed (including as-fast-as-possible)
- **Object tracking** — draw a box around a moving target and track it with OpenCV (MIL, DaSiamRPN, Nano, or Vit)
- **Noise & rate filters** — toggle and tune the sensor's on-chip Anti-Flicker, Event Rate Controller, Trail, and Event Rate Activity filters through the same style of panel
- **Automated bias-sweep suites** — step through a JSON-defined list of bias settings, recording a timed clip at each step
- **MP4 export** and **suite comparison/plotting tools** for offline analysis
- **Virtual webcam output** — send the live rendered view to a virtual camera so other apps (video calls, OBS, browsers) can use it as a normal webcam source

## Requirements

- A Prophesee/Metavision-compatible event camera (for live capture — file playback works without one)
- [OpenEB](https://github.com/prophesee-ai/openeb) built from source (there's no pip package for `metavision_hal`/`metavision_core`)
- Python 3.12 (to match OpenEB's bundled `dist-packages` layout — adjust the paths in `main.py`/`export_mp4.py` if you built against a different version)
- `python3-tk` for the bias/filter control panels:
  ```bash
  sudo apt install python3-tk
  ```
- A virtual camera backend — **only needed for `--virtual-cam`**, skip this if you don't use that flag:
  - **Linux**: [v4l2loopback](https://github.com/umlaeute/v4l2loopback), a kernel module:
    ```bash
    sudo apt install v4l2loopback-dkms
    sudo modprobe v4l2loopback devices=1
    ```
    The `modprobe` step doesn't persist across reboots — see the [v4l2loopback docs](https://github.com/umlaeute/v4l2loopback) if you want it to load automatically.
  - **Windows**: install [OBS](https://obsproject.com/) (ships a virtual camera since OBS 26.0) — no further setup needed.
  - **macOS**: install [OBS](https://obsproject.com/), then do this one-time setup: start OBS, click "Start Virtual Camera", then "Stop Virtual Camera", then close OBS.

## Installation

1. Build and install OpenEB following the [official instructions](https://github.com/prophesee-ai/openeb). By convention this repo assumes it ends up installed at `~/openeb/install`; if yours is elsewhere, set:
   ```bash
   export OPENEB_INSTALL_DIR=/path/to/openeb/install
   ```
2. Clone this repo and install the Python dependencies:
   ```bash
   git clone <this-repo-url>
   cd event-camera-viewer
   pip install -r requirements.txt
   ```

`main.py` and `export_mp4.py` bootstrap the OpenEB environment (`LD_LIBRARY_PATH`, `MV_HAL_PLUGIN_PATH`, `HDF5_PLUGIN_PATH`) automatically and re-exec themselves if it isn't already sourced — you don't need to source anything by hand first.

## Quickstart

Live view from the first camera found:
```bash
python main.py
```

Live view from a specific camera, with custom slice/accumulation/display settings:
```bash
python main.py --serial <SN> --slice-us 10000 --accum-us 20000 --fps 30
```

Play back a recording:
```bash
python main.py --input recording_20260101_120000.hdf5
python main.py --input recording_20260101_120000.raw --speed 2.0   # 2x speed
python main.py --input recording_20260101_120000.raw --speed 0     # as fast as possible
```

Play a folder of recordings as a playlist (use `[` / `]` to move between files):
```bash
python main.py --playlist ./my_recordings/
```

Run an automated bias-sweep suite (press `T` in the viewer to start/stop it):
```bash
python main.py --suite my_suite.json
```

Also expose the live view as a virtual webcam for other apps (see [Requirements](#requirements) for the one-time OS setup); `--virtual-cam` combines with `--input`/`--playlist` too, so a replayed recording can feed a video call the same way:
```bash
python main.py --virtual-cam
```

The virtual camera receives the clean rendered view *before* the on-screen HUD (status bar, badges, hints) is drawn, so timestamps and recording indicators don't show up in whatever app is consuming it — your own window still shows the full HUD as normal. If no virtual camera backend is installed, the viewer prints a warning with setup instructions and keeps running normally without it.

## Keybindings

| Key | Action |
|---|---|
| `Q` / `Esc` | Quit |
| `R` | Start/stop RAW recording (live mode only) |
| `H` | Start/stop HDF5 recording (live mode only) |
| `B` | Open/close the bias control panel |
| `F` | Open/close the noise/rate filter panel |
| Mouse drag | Draw a custom hardware ROI |
| `C` | Clear the active ROI |
| `O` | Cycle through preset center-crop ROIs, then back to full sensor |
| `+` / `=` | Increase accumulation window |
| `-` / `_` | Decrease accumulation window |
| `,` / `<` | Decrease playback speed (file/playlist mode) |
| `.` / `>` | Increase playback speed (file/playlist mode) |
| `Space` | Pause/resume (file/playlist mode) |
| `K` | Draw a box to start object tracking, or stop tracking if active |
| `T` | Start/stop the loaded bias-sweep suite (requires `--suite`) |
| `N` | Skip to the next suite step |
| `S` | Save a snapshot PNG of the current frame |
| `[` / `]` | Previous/next file in a playlist |

## Noise & rate filters

Press `F` (live mode only) to open a panel for the sensor's on-chip filters. Each section only appears if your specific camera/plugin reports that facility — availability varies by sensor, so the panel is built dynamically from whatever the HAL exposes rather than assuming a fixed set:

| Filter | HAL facility | Purpose |
|---|---|---|
| Anti-Flicker | `I_AntiFlickerModule` | Suppresses periodic flicker (e.g. 50/60Hz mains lighting) within a configurable frequency band |
| Event Rate Controller (ERC) | `I_ErcModule` | Caps the sensor's output event rate so high-contrast scenes don't saturate the host/USB link |
| Event Trail Filter | `I_EventTrailFilterModule` | Suppresses redundant repeat events from the same pixel within a threshold window |
| Event Rate Activity Filter | `I_EventRateActivityFilterModule` (via `device.get_i_event_rate()`) | Only propagates a pixel's activity while its local event rate stays within a hysteresis band |

These all run on the camera itself (not in software), so they have no effect on file playback — the panel is unavailable in `--input`/`--playlist` mode. `camera_manager.py` queries each facility live and treats it as absent if unsupported, the same pattern used for biases and ROI.

## Bias-sweep suites

A suite is a JSON file describing a sequence of bias settings to step through automatically, recording a fixed-duration clip at each one:

```json
{
  "duration_s": 5.0,
  "settle_s": 0.5,
  "format": "hdf5",
  "output_dir": "suite_output",
  "settings": [
    { "name": "baseline",         "biases": { "bias_diff_on": 0,   "bias_diff_off": 0 } },
    { "name": "bias_diff_on_-25", "biases": { "bias_diff_on": -25, "bias_diff_off": 0 } },
    { "name": "bias_diff_on_+25", "biases": { "bias_diff_on": 25,  "bias_diff_off": 0 } }
  ]
}
```

- `duration_s` / `settle_s` — default recording length and settle time before recording starts; either can be overridden per-setting with a `"duration_s"` key on that setting.
- `format` — `"hdf5"`, `"raw"`, or `"both"`.
- `output_dir` — where recordings and the resulting `suite_metadata.json` are written.
- Any bias omitted from a setting keeps its current value; biases are offsets from each camera's factory default (`0` = factory default).

These recommended ranges are enforced as hard limits on the bias panel's sliders and entry fields — `camera_manager.py` detects the connected sensor (IMX636 vs GenX320) via the HAL and picks the matching table below, clamping the wider hardware-reported range down to it. On an unrecognised sensor, no table matches and the panel falls back to the camera's full hardware-reported range, unclamped.

Recommended offset ranges for the standard biases on an IMX636 (EVK4HD) sensor, per Prophesee's [Biases documentation](https://docs.prophesee.ai/stable/hw/manuals/biases.html):

| Bias | Recommended |
|---|---|
| `bias_diff` | -25 to 23 *(recommended not to change)* |
| `bias_diff_on` | -85 to 140 |
| `bias_diff_off` | -35 to 190 |
| `bias_fo` | -35 to 55 |
| `bias_hpf` | 0 to 120 |
| `bias_refr` | -20 to 235 |

**GenX320** uses a different bias model: values are absolute (not offsets from a factory default), and defaults differ between the ES (Engineering Sample) and MP (Mass Production) chip revisions. Recommended ranges, from the same [Biases documentation](https://docs.prophesee.ai/stable/hw/manuals/biases.html):

| Bias | Recommended range | Default (ES) | Default (MP) |
|---|---|---|---|
| `bias_diff` | 41 to 51 *(recommended not to change)* | 51 | 51 |
| `bias_diff_on` | 24 to 60 | 40 | 25 |
| `bias_diff_off` | 19 to 50 | 40 | 28 |
| `bias_fo` | 19 to 39 | 29 | 34 |
| `bias_hpf` | 0 to 127 | 0 | 40 |
| `bias_refr` | 0 to 127 | 82 | 10 |

`bias_pr` (listed in Features above) is not available on IMX636, Gen4.1, or GenX320 sensors per the same source — the viewer's bias panel simply omits it if your camera doesn't report it.

Each run writes its recordings plus a `suite_metadata.json` describing what was captured, which `analyze_suite.py` consumes.

## Analysis tools

`export_mp4.py` exports an HDF5 or RAW recording to MP4, using the same rendering as the live viewer:
```bash
python export_mp4.py --input recording.hdf5
```

`analyze_suite.py` compares and plots suite output, via subcommands:

| Subcommand | Purpose |
|---|---|
| `compare` | Text-table diff of total events per bias setting across two suite runs.<br>`python analyze_suite.py compare a/suite_metadata.json b/suite_metadata.json --label-a before --label-b after` |
| `rates` | Plot ON/OFF event rate over time for every step in one or two suites.<br>`python analyze_suite.py rates suite_metadata.json` |
| `sweep` | Plot average ON/OFF event rate vs. the swept bias value.<br>`python analyze_suite.py sweep suite_metadata.json` |
| `spatial` | Spatial, polarity, and temporal comparison between two suite runs.<br>`python analyze_suite.py spatial a/suite_metadata.json b/suite_metadata.json --output plots/` |

All subcommands accept `--label-a`/`--label-b` to name the runs being compared; run `python analyze_suite.py <subcommand> --help` for the full set of options.

## Repository layout

```
event-camera-viewer/
├── main.py              Live viewer / playback CLI entry point
├── camera_manager.py    HAL device wrapper (biases, ROI, RAW recording)
├── visualizer.py        OpenCV display, controls, recording
├── hdf5_reader.py       Custom HDF5 event format reader
├── hdf5_writer.py       Custom HDF5 event format writer
├── raw_reader.py        Prophesee .raw file playback
├── playlist.py          Multi-file playlist iterator
├── suite_runner.py      Automated bias-sweep suite runner
├── tracker.py           OpenCV object-tracking wrapper
├── export_mp4.py        HDF5/RAW → MP4 export
├── analyze_suite.py     Suite comparison/plotting (compare/rates/sweep/spatial)
├── requirements.txt
└── LICENSE
```

## License

MIT — see [LICENSE](LICENSE).
