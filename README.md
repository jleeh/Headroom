# Headroom

Safe, automatic overclocking for Windows gaming PCs: a GPU auto-overclock with its own stress tests, a GPU fan curve, and a guided AMD Ryzen Curve Optimizer tune. It measures the speed-up before and after, and works out what that means for games.

**Install:** run `HeadroomSetup-<version>.exe`. It asks for admin rights, because changing clocks needs them. When it finishes, Headroom starts, scans the hardware and builds a profile for that exact PC. No Python or other software is needed.

## What it does

| | How | Limits that are never crossed |
|---|---|---|
| **GPU** (NVIDIA RTX) | Searches core and memory clock offsets through NVIDIA's NVML. Each step is checked bit for bit against a stock run by Headroom's own CUDA stress test, and driver resets count as failures. A combined soak and a safety margin come after the search. | No voltage or power-limit changes. Back to stock at the core or memory-junction temperature limit (default 83 °C / 96 °C). Offset caps per GPU family. |
| **Crash watch** | Windows starts Headroom's handler on NVIDIA driver resets, WHEA hardware errors, bluescreens and game crashes inside the NVIDIA driver. A GPU crash with an overclock applied backs it off (-15 MHz core, then -250 memory); repeated crashes put the GPU back to stock. CPU errors are mapped to the core whose Curve Optimizer value needs loosening. A flight recorder keeps the last two minutes of telemetry for each crash. | Never raises anything. CPU settings live in the BIOS and are only reported, never changed. |
| **Re-checks** | A driver or VBIOS update, warmer weather (idle GPU much hotter than when tuned) or a 90-day-old result prompts a quick re-check of the saved offsets, with back-off if they no longer hold. | |
| **GPU fans** | A fan curve with separate core-temperature and VRAM-temperature curves; the fans run at whichever asks for more. The card's own fan control (including zero-rpm at idle) takes over when it's cool. Pauses itself while another fan controller (Glint, FanControl, MSI Afterburner, iCUE, ...) is running, instead of fighting it. | Forced 100 % at 80 °C core / 94 °C VRAM. Fans go back to the card's control when the curve is turned off, on reset and on exit, and if the fan process dies. |
| **CPU** (AMD Ryzen 5000/7000/9000) | PBO + per-core negative Curve Optimizer. CoreCycler finds the per-core values; Headroom shows the exact BIOS sheet, then verifies and measures. | Power limits, Vcore and SoC voltage are never raised. The stress test stops on the temperature limit, or if the chosen pump / CPU fan headers slow down. |
| **RAM** | Advice only (e.g. "EXPO looks off", "4 sticks at this speed is already past the rating"). | Never changed. |
| **Intel 13th/14th gen** | No tuning; reads the CPU's microcode and warns if it's older than 0x12B (the Vmin-shift degradation fix). | Never changed. |
| **Games** | *Measure a game* (Benchmarks tab) alternates stock and tuned offsets while you play and records real frame rates with [PresentMon](https://github.com/GameTechDev/PresentMon), so both are measured in the same session. *When to overclock* can apply the offsets only while chosen games run. | |

## Hardware profiles

On first start Headroom scans the PC: CPU, motherboard and BIOS, GPU and its board partner, driver and power limits, memory kits, sensors and fans. It then builds the active profile from the best source it has for each part:

1. **Research with Claude** (Setup tab, optional). Claude searches reviews and owner results for the exact parts, then writes a profile with source links. It runs on either:
   - **your Anthropic API key** (stored encrypted with Windows DPAPI, billed to your account; Headroom shows the estimated cost per run, tracks what it spent this month and stops at a monthly cap you set), or
   - **your own Claude Code login** (`claude -p`, counts against your Claude plan; started without admin rights and with web search/fetch as its only tools; check Anthropic's terms on using a subscription this way).

   Only model names, versions and limits from the scan are sent. Only URLs that were actually retrieved are kept as sources, and BIOS settings outside what Headroom does (voltages, power limits, memory) never reach the BIOS sheet.
2. **Built-in research** (`profiles/builtin/`), for parts that have already been researched.
3. **Family guidance** (`profiles/families.json`): conservative ranges for each GPU or CPU generation.

Whatever the source, `headroom/profiles.py` clamps the safety limits to hard caps. Headroom's own tests decide the final values.

## Requirements

- Windows 10/11 64-bit with the Microsoft Edge WebView2 Runtime (already part of Windows 11).
- Sensors: CPU temperature, GPU memory temperature and fan speeds come from [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) with its web server on port 8085. The Setup tab can download and configure it. Recent versions need the [PawnIO](https://pawnio.eu) driver.
- CPU tuning downloads [CoreCycler](https://github.com/sp00n/CoreCycler) when you press *Install CoreCycler*; game measurement downloads PresentMon. Every downloaded tool is pinned to an exact release and checked by SHA-256.

## Building from source

```powershell
# needs 64-bit Python 3.12 (python.org or `winget install Python.Python.3.12`)
powershell -ExecutionPolicy Bypass -File installer\build.ps1 -Version 1.1.0 [-CertThumbprint <sha1>]
# -> dist\HeadroomSetup-1.1.0.exe (+ .sha256)
```

The build script downloads the WebView2 SDK from NuGet and compiles `Headroom.exe` and the installer with the C# compiler that ships with Windows. It also copies a private Python runtime (isolated with a `._pth` file) and installs the Anthropic SDK into `vendor/` from `installer/requirements.txt`. Every download is pinned by SHA-256, and the build stops on a mismatch.

To run from a checkout: `installer\build.ps1 -ExeOnly` (WebView2 DLLs + `Headroom.exe` next to the sources), then
`py -3.12 -m pip install --require-hashes --no-deps -r installer\requirements.txt --target vendor` and start `Headroom.exe`.

Releases with update checks: create a signing key once with `installer\sign_update.ps1 -NewKey -KeyFile <outside the repo>`, paste the printed public key into `headroom/update.py` (`UPDATE_PUBKEY`), and attach the `update.json` + `update.json.sig` that `sign_update.ps1 -Installer ...` writes to each GitHub release.

### Tests

```
python -m pip install pytest cryptography ruff
ruff check . && python -m pytest -q          # runs on Windows and Linux; Windows-only APIs are faked
```

`dev/selftest_cuda.py` and `dev/selftest_admin.py` exercise the real GPU (the latter as administrator); `dev/ui_smoke.py` renders every tab in headless Chromium against seeded data.

Installer switches: `/S` silent, `/D=C:\path` install folder, `/extract <dir>` unpack only.
Uninstall from *Settings → Apps*, or run `Uninstall.exe /uninstall [/S] [/removedata]`. Uninstalling puts the GPU back to stock, gives the fans back to the card and removes Headroom's scheduled tasks.

## Layout

```
launcher/              Headroom.cs: WebView2 window + launcher (elevated), starts the Python backend
bin/                   entry points
  headroom_server.pyw      backend: API + UI on 127.0.0.1:7871
  headroom_fans.pyw        fan-curve process (keeps running with the window closed)
  headroom_apply.pyw       applies the saved GPU profile at logon (skipped after a crash), then runs the monitor
  headroom_monitor.pyw     flight recorder, re-check flags, per-game apply, stale-fan watchdog
  headroom_crash.pyw       crash handler, started by Windows on driver resets / WHEA errors / bugchecks
  headroom_cpuwatch.pyw    headless CPU thermal watchdog while a Curve Optimizer search is running
  headroom_research_cli.pyw  unelevated runner for research through Claude Code
  headroom_reset.pyw       GPU to stock + fans to auto (used by the uninstaller)
headroom/              scan, profiles, research(_cli), gpu_tune, cpu_tune, fan_curve, stability, presentmon,
                       update, nvml, cuda_stress, sensors, uplift, server
web/                   index.html + app.js: the UI
profiles/              builtin/ researched profiles, families.json fallback guidance
installer/             Setup.cs (installer + uninstaller), build.ps1, requirements.txt, sign_update.ps1
assets/                icons
tests/                 pytest suite        dev/   hardware self-tests, UI smoke test
```

Data lives in `%APPDATA%\Headroom` (profile, scan, state, logs). Downloaded tools go in `tools\` inside the install folder, which only administrators can change, because they run elevated.

## Security

See [SECURITY.md](SECURITY.md) for how the elevated backend is protected and how to report a vulnerability.

## Disclaimer

Overclocking runs hardware outside its factory settings. Headroom is conservative by design, but crashes or freezes during testing are expected, and some manufacturers don't cover overclocking under warranty. Use at your own risk (see LICENSE).
