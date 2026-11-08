# Third-party components

Bundled in the installer:

| Component | Licence | Where |
|---|---|---|
| Python 3.12 runtime | PSF License (`runtime/LICENSE.txt`) | `runtime/` |
| Anthropic Python SDK and its dependencies (httpx2, pydantic, anyio, ...) | MIT / BSD / Apache-2.0 (see each package's `*.dist-info`) | `vendor/` |
| Microsoft Edge WebView2 SDK (`Microsoft.Web.WebView2.*.dll`, `WebView2Loader.dll`) | Microsoft WebView2 SDK licence (redistributable) | app folder |

Downloaded on the user's PC only when they press the button for it (not bundled), pinned to an exact release and checked by SHA-256:

| Component | Licence | Source |
|---|---|---|
| LibreHardwareMonitor | MPL-2.0 | github.com/LibreHardwareMonitor/LibreHardwareMonitor |
| CoreCycler | see its repository | github.com/sp00n/CoreCycler |
| PresentMon (frame-time capture for game measurements) | MIT | github.com/GameTechDev/PresentMon |
| PawnIO (driver used by both) | see pawnio.eu | pawnio.eu |
