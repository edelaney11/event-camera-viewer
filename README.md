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
- Python — `main.py`/`export_mp4.py` auto-detect their own interpreter's version (`python{major}.{minor}`) to locate OpenEB's `dist-packages`, so this works regardless of which Python version OpenEB was built against, as long as it's the same one running the viewer
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

### Raspberry Pi (GenX320 Starter Kit)

The [Prophesee GenX320 Starter Kit for Raspberry Pi 5](https://www.prophesee.ai/event-based-starter-kit-genx320-raspberry-pi-5/) connects the sensor over the **CSI ribbon cable**, not USB, and talks to it through a **V4L2 HAL plugin** rather than the standard USB path. This needs a different OpenEB build than the generic instructions above:

1. Build OpenEB with the RPi patch from [`prophesee-ai/rpi-sensor-drivers`](https://github.com/prophesee-ai/rpi-sensor-drivers) applied (its README covers cloning OpenEB, applying `openeb-for-rpi.patch`, and building) — this is a **separate build** from a plain/unpatched OpenEB checkout, since the plain build has no V4L2/CSI support at all.
2. Point `OPENEB_INSTALL_DIR` at wherever *that* patched build actually installed to (check the `-DCMAKE_INSTALL_PREFIX` you used, or wherever `sudo make install` reported — a bare `sudo make install` with no custom prefix typically lands in `/usr/local`, not under your home directory).
3. Every boot, before running the viewer, load the sensor's kernel driver and set up its V4L2 pipeline:
   ```bash
   sudo dtoverlay genx320,cam0     # or ,cam1, depending on which CSI port
   ~/rpi-sensor-drivers/rp5_setup_v4l.sh
   ```
   These are kernel/shell-level steps that `main.py` has no way to do itself. `main.py` does automatically set the two runtime environment variables the V4L2 plugin needs (`PSEE_VAR_V4L2_BSIZE=1`, `V4L2_HEAP=vidbuf_cached`), so nothing else needs to be sourced.

If `python main.py` reports `ModuleNotFoundError: No module named 'metavision_hal'`, double check `OPENEB_INSTALL_DIR` is pointing at the RPi-patched build and not a plain one — it's easy to end up with both on disk (e.g. `~/openeb` and `~/openeb-rpi`) and have the wrong one picked up by default.

### Headless recording (Raspberry Pi)

`main.py` needs a real display and keyboard — it opens an OpenCV window and Tk bias panels, and recording is toggled with the `R`/`H` keys. That's the wrong shape for a Pi with no monitor attached. For that case, use `record_headless.py` instead: it opens the camera and starts RAW recording immediately with no GUI at all, then blocks until it receives SIGINT/SIGTERM.

```bash
python record_headless.py --output-dir ~/recordings [--serial <SN>] [--bias-file biases.json]
```

`--bias-file` is optional — a flat JSON file of `{"bias_name": value, ...}` applied before recording starts, since there's no bias panel to use headlessly.

To control it remotely over SSH, wrap it in the systemd units under [`systemd/`](systemd/):

1. Copy both unit files to `/etc/systemd/system/`, adjusting the placeholder paths inside each (CSI port `cam0`/`cam1`, `rpi-sensor-drivers` location, repo/venv path, `--output-dir`) to match your install.
2. `sudo systemctl daemon-reload && sudo systemctl enable --now genx320-setup` — this runs the `dtoverlay`/`rp5_setup_v4l.sh` steps from the previous section automatically on every boot, so the sensor is ready before you ever SSH in.
3. Start/stop a recording from any machine on the network:
   ```bash
   ssh pi@<host> sudo systemctl start genx320-record   # begin recording
   ssh pi@<host> sudo systemctl stop genx320-record     # stop and finalize the file
   ssh pi@<host> systemctl status genx320-record        # is it currently recording?
   ssh pi@<host> journalctl -u genx320-record -f        # follow its log live
   ```
`genx320-record` is intentionally left disabled (not started on boot) so recording is something you trigger on demand — enable it too (`systemctl enable`) only if you actually want it to start recording automatically at power-on.

### Remote live view (Raspberry Pi over the network)

`main.py --tcp HOST:PORT` connects to *any* camera being streamed over a plain TCP socket, instead of opening a local camera or file — the same mechanism already used for a Prophesee Onboard (see [`onboard_streamer/tcp_event_streamer.cpp`](onboard_streamer/tcp_event_streamer.cpp)). [`genx320_streamer.py`](genx320_streamer.py) is a pure-Python server implementing the same wire protocol for the GenX320 (or any camera this repo's `camera_manager.py` can open) — useful when the Pi has no display attached and you'd rather view/record from your main machine than run `main.py` directly over SSH + X-forwarding.

On the Pi (after the usual `dtoverlay`/`rp5_setup_v4l.sh` boot steps from the previous section):
```bash
python genx320_streamer.py --port 9000
```

From any other machine on the same network, running a plain OpenEB build (no RPi V4L2 patch needed — the client never touches the sensor directly):
```bash
python main.py --tcp <pi-ip>:9000
```
This gets you the full live viewer — bias/ROI panels, recording, tracking — driven by events arriving over the network, exactly as if the GenX320 were plugged directly into your machine. `record_headless.py`'s own RAW recording and `genx320_streamer.py` both need exclusive access to the camera, so don't run both at once.

`genx320_streamer.py` doesn't start pulling events from the camera until a client actually connects (and discards anything already queued if a client reconnects later) — it's fine to leave it running on the Pi for a long time before you ever open the viewer; it won't hand you a backlog of everything that happened while nobody was watching.

By default the connection is *live view, lossy*: if the camera produces events faster than the link to the viewer can carry (common over Wi-Fi with a busy scene), the oldest queued events are dropped to keep lag bounded rather than let a backlog grow. Press `H` in the viewer to start HDF5 recording — this tells the server to switch to lossless capture (preserving its backlog across any reconnect) for as long as you're actually recording, and it switches back to live/lossy once you stop. (RAW recording's `R` key doesn't do this — RAW recording needs direct hardware access, which `--tcp` mode doesn't have; use HDF5 recording over the network.)

If you see repeated `DATA LOSS`/`falling behind` messages from `genx320_streamer.py`, that's either the camera outpacing the network link, or (check CPU usage on the Pi — `top`, or `py-spy top --pid <pid>` for a breakdown) `genx320_streamer.py` itself maxing out a core decoding events, in which case see "If genx320_streamer.py itself is the bottleneck" below before reaching for these. Otherwise, two ways to address a genuine link-bandwidth problem:
- `--max-rate KEV_S` caps event production at the sensor itself (its on-chip Event Rate Controller), the right fix if it's a sustained, not just bursty, overload.
- `--live-queue-cap N` (default 500,000) controls how much backlog builds up before dropping while not recording — lower it for tighter live lag, raise it to absorb bigger bursts. While actively recording, there's no cap at all by default (`--max-queued-events` defaults to 0 = unlimited) — every event is kept and delivered no matter how large the backlog grows, since recording means you need all of it. That trades away OOM protection: if the camera sustainably outpaces the link for long enough, memory usage grows without bound, and the process could eventually be killed for OOM — which would lose the whole recording, not just the overflow. Pass `--max-queued-events` a nonzero value to accept bounded, loud data loss instead of that risk, or use `--max-rate` so the imbalance this is meant to protect against doesn't happen in the first place.

To run it as a systemd service the same way as `genx320-record` (see above), use [`systemd/genx320-streamer.service`](systemd/genx320-streamer.service) in place of `genx320-record.service` — same setup steps, same `genx320-setup` dependency, just swap which unit you enable.

#### If genx320_streamer.py itself is the bottleneck

On a Raspberry Pi, decoding events (parsing the sensor's native EVT format into `(x, y, p, t)`) is genuinely CPU-heavy, and Python's GIL serializes that decode work against `genx320_streamer.py`'s own network-send loop on a single core — so even a multi-core Pi can end up bottlenecked on one core, with the queue backing up and dropping even though the network link itself has headroom. If `py-spy` (`pip install py-spy`, then `sudo py-spy top --pid $(pgrep -f genx320_streamer.py)`) shows most of the time inside `metavision_core`'s own decode call rather than anything in `genx320_streamer.py`'s own code, that's this.

[`genx320_streamer_native/`](genx320_streamer_native/) is a C++ equivalent — same wire protocol (`main.py --tcp` and `network_reader.py` need no changes to use it instead), same `--max-rate`/`--live-queue-cap`/`--max-queued-events` flags and recording-toggle behavior, built against OpenEB's own C++ SDK (`metavision_sdk_stream`, not the closed-source SDK `onboard_streamer/` uses). It pays the same native decode cost per event, but the camera's callback thread and the network-send thread are genuine OS threads with no GIL between them, so they can actually run concurrently on separate cores instead of serializing. Build it (needs the same OpenEB install already used for everything else — see `-DCMAKE_PREFIX_PATH` below if it's not found automatically):

```bash
cd genx320_streamer_native
mkdir build && cd build
cmake -DCMAKE_PREFIX_PATH=$OPENEB_INSTALL_DIR ..   # omit -D... if ~/openeb/install is already on the default search path
make
./genx320_streamer --port 9000
```

To run it as a systemd service, use [`systemd/genx320-streamer-native.service`](systemd/genx320-streamer-native.service) in place of `genx320-streamer.service` — same idea, just pointing at the compiled binary instead of the Python script.

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

Playing back a single `.raw` file also adds a seek bar near the bottom of the HUD — click or drag it to scrub to any point in the file. (RAW files only, since seeking relies on the SDK's `RawReader`; not available for HDF5 or `--playlist` playback.) On first use it reads the file's full duration, which the SDK caches to a `<name>_info.json` sidecar so later runs (including `X` to split) skip the re-scan.

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
| `M` | Rename the last completed recording |
| `P` | Mark a split point at the current playback position (file mode only) |
| `Backspace` | Undo the most recent split mark |
| `X` | Split a RAW recording into multiple files — uses marked points if any, otherwise prompts for part lengths |
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
├── record_headless.py   No-GUI RAW recording for headless/remote deployments
├── genx320_streamer.py  No-GUI TCP event streaming for headless/remote deployments (Python)
├── genx320_streamer_native/  Same, in C++ — for when genx320_streamer.py itself is the CPU bottleneck
├── network_reader.py    TCP client for any of the above / tcp_event_streamer.cpp (--tcp)
├── onboard_streamer/    C++ TCP event streamer for a Prophesee Onboard
├── systemd/             Unit files for record_headless.py/genx320_streamer(_native) on a headless Raspberry Pi
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
