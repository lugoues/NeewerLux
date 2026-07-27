# NeewerLux

A headless Neewer LED light controller. Runs as a service, talks to the lights over
Bluetooth LE, and is driven entirely through its HTTP API and web dashboard. Includes a
keyframe animation engine with 101 built-in animations, a preset system, and parallel
BLE writes.

There is no desktop GUI. If you want a window, use the upstream project this forks from.

Fork of [NeewerLite-Python](https://github.com/taburineagle/NeewerLite-Python) (v0.12d) by [@taburineagle](https://github.com/taburineagle), originally based on [NeewerLite](https://github.com/keefo/NeewerLite) by [@keefo](https://github.com/keefo) (Xu Lian).

**NeewerLux is not affiliated with or endorsed by Neewer.**

**Supported lights:** GL1, NL140, SNL1320, SNL1920, SNL480, SNL530, **SNL660**, SNL960, SRP16, SRP18, WRP18, ZRP16, BH30S, CB60, CL124, RGB C80, RGB CB60, RGB1000, RGB1200, RGB140, RGB168, RGB176 A1, RGB512, RGB800, SL-90, RGB1, **RGB176**, RGB18, RGB190, RGB450, **RGB480**, RGB530 PRO, RGB530, RGB650, **RGB660 PRO**, RGB660, RGB960, RGB-P200, RGB-P280, SL-70, **SL-80**, ZK-RY

---

## Installation

Requires Python 3.11 or newer, and a Bluetooth adapter with BlueZ on Linux.

```
uv sync --locked
uv run NeewerLux.py
```

With no arguments NeewerLux starts the HTTP server and serves the dashboard at
http://localhost:8080/. Dependency versions are pinned in `uv.lock`, so this reproduces
exactly what the release is built against.

Using pip instead:
```
pip install "bleak>=0.22,<4"
python NeewerLux.py
```

### Other modes

| Command | What it does |
|---------|--------------|
| `NeewerLux.py` | Start the HTTP server and dashboard (the default) |
| `NeewerLux.py --http` | The same thing, stated explicitly |
| `NeewerLux.py --list` | Scan for nearby lights and print them |
| `NeewerLux.py --cli --light <MAC> --mode CCT --temp 56 --bri 50` | Send one command and exit |

### Windows

Download the release zip and run `NeewerLux.exe`, or `NeewerLux-HTTP.bat`. It is a
console application, not a windowed one.

Preset and animation files live in `light_prefs/` alongside the executable and can be
edited with any text editor.

---

## Features

### Custom Animation System

A full keyframe-based animation engine that drives Neewer lights through timed color sequences with smooth interpolation. Animations are stored as JSON files in `light_prefs/animations/` and can target individual lights by MAC address, numeric ID, alias name, or all lights with the `"*"` wildcard.

Each keyframe specifies a hold time (how long to dwell on the color), a fade time (how long to transition from the previous keyframe), and per-light color parameters in HSI, CCT, or Scene modes. The engine handles shortest-path hue interpolation around the 360° color wheel so transitions between, say, red (0°) and magenta (300°) go the short way rather than sweeping through the entire spectrum.

**Playback controls:**
- **Speed** — multiplier applied to all timing values (0.25x to 4x)
- **Rate** — BLE updates per second during fades (1-30, default 5; with Parallel enabled, ~15 is achievable regardless of light count)
- **Brightness** — scales all brightness values at playback time without modifying the animation file (5-100%)
- **Loop** — continuous playback with seamless wraparound fading between last and first keyframe
- **Parallel writes** — sends BLE commands to all lights simultaneously using `asyncio.gather()` instead of sequentially, reducing per-frame time from ~50ms×N to ~50ms regardless of light count (toggle-able for legacy adapter compatibility)

**101 built-in animation presets** across categories:

| Category | Examples |
|----------|---------|
| Emergency | Police Flash, Ambulance, Fire Truck, Hazard |
| Rock Performance | Guitar Solo, Drum Solo, Metal Mosh, Encore, Power Ballad, Spotlight, Rock Anthem, Concert Build |
| Holidays | Christmas, Halloween, Valentine's, Easter, Hanukkah, New Year's Eve, St. Patrick's, Fourth of July |
| Practical/Studio | Interview, Warm Studio, Focus, Reading Light, Product Photo, Film Noir, Key Fill Rim, Dawn Simulator, Magic Hour, Golden Hour |
| Multi-Light Utility | Color Chase, Ping Pong, Ripple, Alternating Flash, Gradient Sweep, Warm Cascade, Identify Lights |
| Smooth/Ambient | Concert Sweep, Neon Nights, Retrowave, Stage Wash, Fire Flicker, Campfire, Candlelight, Sunset Fade, Ocean Waves, Northern Lights, Lava Lamp, Breathe, Color Wash, Color Cycle, Rainbow Gradient, Rainbow Chase, and many more |

Animations are JSON files in `light_prefs/animations/` and can be written by hand or
generated from the six built-in templates. Six template generators ship with the code.

### Preset System

Presets are stored in `light_prefs/customLights.prefs` and exposed on the dashboard and
over the HTTP API:
- **Save Current Settings** — capture current slider positions
- **Rename** — custom preset names (also via middle-click)
- **Move Left/Right** — reorder presets
- **Duplicate Preset** — deep-copy with "(copy)" suffix
- **Delete Preset**

Ships with 8 default presets: Warm Studio, Daylight, Cool White, Candlelight, Red Alert, Blue Mood, Purple Haze, Green Screen.

### Global CCT Range

Configurable minimum and maximum colour temperature bounds set in the preferences file
(2700K-8500K, default 3200K-5600K). Per-light overrides in a light's sidecar file take
precedence for that light.

### CCT Clamping & Incompatibility Handling

Software-side enforcement of CCT temperature bounds on all BLE write paths:
- **Convert/Clamp** — out-of-range values clamped to the light's effective range
- **Ignore/Skip** — out-of-range commands silently dropped

Also handles HSI/Scene commands sent to CCT-only lights. Ensures consistent behavior across mixed light setups.

### Light Aliases (Preferred ID)

Each light's sidecar file in `light_prefs/` can carry a **Preferred ID** (0-99) alongside a custom name:
- Lights are listed preferred-ID first, in ID order
- Animation keyframes can use names (e.g., `"Key"`, `"Fill"`) as light targets
- HTTP batch commands work with names: `?batch=Key:HSI:0:100:50;Fill:CCT:56:80`
- Preferred IDs resolve consistently regardless of BLE discovery order

### WebUI Dashboard

A browser-based control panel at `http://localhost:8080/`:
- Live light table with status
- CCT/HSI/Scene controls with sliders
- Preset grid with add/delete endpoints
- Animation browser with categorized sections and play/stop
- Update checker
- Collapsible API reference

### HTTP Animation API

**GET:** `http://server:port/NeewerLux/doAction?animate=Concert%20Sweep|2.0|10|50`

**POST** to `/NeewerLux/doAction`:
```json
{
  "action": "play",
  "name": "Concert Sweep",
  "speed": 1.0,
  "loop": true,
  "rate": 10,
  "brightness": 50,
  "parallel": true
}
```

### Additional Features

- **Update checker** — GitHub Releases API, shown on the dashboard
- **Instance lock** — PID-based, with stale lock detection
- **Logging** — buffered writes to `light_prefs/NeewerLux.log`
- **Auto-reconnect on wake** — the background worker re-links lights after sleep
- **Parallel BLE writes** — `asyncio.gather()` for simultaneous multi-light commands

---

## Repository

https://github.com/poizenjam/NeewerLux/

## License

Same as upstream — see [NeewerLite-Python](https://github.com/taburineagle/NeewerLite-Python) for license details.
