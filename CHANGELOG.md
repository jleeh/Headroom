# Changelog

## 1.1.0

**Security**
- POST bodies must be JSON (refuses cross-site "simple" requests), body size capped; page script moved to
  `web/app.js` under `script-src 'self'`; BIOS sheet, watchdog messages and research text HTML-escaped.
- Downloaded tools (LibreHardwareMonitor, CoreCycler, PresentMon) pinned to exact releases and SHA-256 checked;
  LibreHardwareMonitor zip-slip guarded; the 1.0.0 sensor logon task that ran from `%LOCALAPPDATA%` is removed.
- Temperatures only trusted from an elevated `LibreHardwareMonitor.exe` on port 8085.
- Every GPU offset clamped to the hard caps before it reaches the card (UI, saved state, logon task, back-off).
- Research: BIOS-sheet allowlist (voltages, power limits, memory and unknown settings are left off and shown
  separately); unsafe source URLs dropped.
- Installer: refuses to install into a non-empty folder that isn't Headroom; uninstall deletes only what it
  installed (manifest) instead of the whole folder; backend stopped through an admin-only named event.
- Signed update checks (ECDSA P-256 key pinned in the app; the installer must match the signed SHA-256).

**Fixes**
- An empty CoreCycler path could make the emergency stop kill every process.
- `state.json` is shared by several processes: the backend no longer overwrites what the logon task, crash
  handler or monitor wrote.
- A freeze during a VRAM soak run no longer marks the core offset unstable.
- Research: resuming after `pause_turn` no longer drops earlier turns; cache-write tokens are counted.
- A killed fan process can no longer leave the fans stuck at a fixed speed.

**New**
- Crash watch with automatic back-off, revert-to-stock after repeated crashes, per-core CPU error mapping,
  bugcheck/minidump attribution and a flight recorder.
- Re-check prompts after driver/VBIOS changes, warmer weather or 90 days, with a quick re-check mode.
- Fan curve pauses while another fan controller runs (Glint, FanControl, MSI Afterburner, iCUE, ...).
- Game measurement with PresentMon (stock vs tuned in the same session); overclock only while chosen games run.
- Research through your own Claude Code login (`claude -p`, unelevated), cost estimate, monthly spend cap, cancel.
- Headless CPU thermal watchdog during Curve Optimizer search/verify, even if Headroom doesn't start.
- Intel 13th/14th gen microcode check (0x12B Vmin-shift fix).
- Tests (pytest, Linux + Windows) and CI; tidier layout (`bin/`, `launcher/`, `assets/`).

## 1.0.1
Security hardening (local API token, Host/Origin checks, CSP, admin-only tools folder, pinned build).

## 1.0.0
First release.
