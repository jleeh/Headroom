# Security policy

Headroom runs with administrator rights and changes hardware settings, so security reports are taken seriously.

## Reporting a vulnerability

Please **don't open a public issue**. Use GitHub's private reporting instead: the repository's **Security** tab → **Report a vulnerability**. Include the version, what an attacker could do, and steps to reproduce. You should get a reply within a week.

## Supported versions

Only the latest release gets security fixes.

## Security design

- **Local API:** the backend listens only on `127.0.0.1`. Every API request needs a random per-launch token that only the Headroom window knows (it is passed to the backend in its environment, never written to disk, and added to requests by the window, so page script never sees it). Requests whose `Host` or `Origin` isn't Headroom's are refused, and POST bodies must be JSON, which blocks web pages (CSRF, DNS rebinding, cross-site "simple" requests) and non-admin programs. The installer stops the backend through a named event that only elevated processes can signal.
- **UI:** a strict Content-Security-Policy (`script-src 'self'`: all script is in `web/app.js`) stops injected markup from running script, and everything from profiles, research and sensors is HTML-escaped. Profile text from web research is stripped of markup, and only `https` links are kept. The window never navigates away from Headroom's own page, and it only opens `http(s)` links, in the normal browser.
- **Research output is data, not instructions:** BIOS settings suggested by research must match an allowlist of what Headroom does (PBO, boost override, Curve Optimizer, thermal limit); anything touching voltages, power limits, memory or unknown settings is left off the BIOS sheet and shown separately. Numeric limits are clamped like every other source.
- **Claude Code backend:** when research runs through the user's own Claude Code login, `claude` is started through a one-shot scheduled task with a limited (non-admin) token and only the WebSearch/WebFetch tools; the elevated backend reads its output as untrusted data.
- **Files that run elevated** (the app, its Python runtime, and the downloaded LibreHardwareMonitor, CoreCycler and PresentMon) live in the install folder. The installer locks that folder so only administrators can change it, even when a custom folder is chosen, and refuses to install into a non-empty folder that isn't Headroom; uninstall removes only what it installed. Downloaded tools are pinned to exact releases and checked by SHA-256 before use. A 1.0.0 sensor task that ran from `%LOCALAPPDATA%` is removed.
- **Sensors:** temperatures that the safety watchdogs act on are only trusted from an elevated `LibreHardwareMonitor.exe` listening on port 8085 (the owner is checked), so a program that grabs the port can't feed fake readings.
- **User-writable data** (`%APPDATA%\Headroom`) is treated as untrusted. Safety limits in `profile.json` are clamped to hard caps when loaded. Every GPU clock offset is clamped to the hard caps before it is applied and checked again at the NVML layer, wherever the request came from (UI, saved state, logon task, crash back-off).
- **Updates:** an update is only offered when `update.json` verifies against the ECDSA P-256 public key built into Headroom; the installer it names must match the signed SHA-256. Nothing installs without the user pressing the button.
- **API key:** stored encrypted with Windows DPAPI for the current user, and never returned by the API or logged.
- **Build:** the WebView2 SDK and all Python packages are pinned by SHA-256 (`installer/build.ps1`, `installer/requirements.txt`).
