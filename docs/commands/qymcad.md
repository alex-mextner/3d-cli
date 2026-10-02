# `3d qymcad`: an explicit local alternative to Fusion

QymCAD is a separate local GUI application, not a replacement implementation of Fusion's automation API. The integration discovers and explicitly launches it. It does not silently retry a failed Fusion operation, copy Fusion feature history, open a supplied document through an undocumented flag, or claim headless editing/CAM support.

```powershell
.\.venv\Scripts\python.exe bin/3d qymcad doctor --json
.\.venv\Scripts\python.exe bin/3d qymcad launch --dry-run --json
.\.venv\Scripts\python.exe bin/3d qymcad launch --json
```

Discovery order: `--executable PATH`, `QYMCAD_EXECUTABLE`, PATH, then the platform-specific local installation. On Windows, managed portable releases live under `~/.local/share/3d-cli/qymcad/<release>/qymcad.exe`. Paths with spaces are passed as one argument; no shell is used. The environment variable is an executable path, not a command string.

`doctor` returns 0 when the executable is present and 127 when absent, with machine-readable capabilities either way. Discovery is not proof that an arbitrary configured binary is genuine or that a GUI launch will succeed. `launch` detaches the GUI, records a startup log, and checks that it survives a 0.75-second startup probe before reporting STARTED and a PID. This is not a full GUI health check; verify the visible window before treating application startup as complete. `--dry-run` reports the argument list and never starts a process.

Open or import a document using QymCAD's own interface. For a neutral faceted STEP produced locally from STL, see [solid](solid.md). STEP exchange does not recover an original Fusion timeline.

QymCAD is AGPL-3.0-or-later and remains an external application. No QymCAD source code was copied into 3d-cli's MIT implementation. Upstream: https://github.com/QymIs-Tech/QymCAD . Official release used for the initial native Windows launch: `v0.1.0-dev.20261001`; SHA-256 `332521c18b7b60cdbf8215c27be5e63875ffe8010a1bad22cd46a002429b1c2d`, verified against the release asset digest.

No subscription or cloud backend is required by this integration. QymCAD may contact its update service; disable that in the application's settings for offline operation. The CLI does not silently change those settings. Headless editing and CAM are explicitly unsupported in the capability report.

On Ubuntu, configure an executable QymCAD AppImage with `--executable` or `QYMCAD_EXECUTABLE`, or place a `qymcad` executable on PATH. Automatic discovery of the managed portable cache is currently Windows-specific. An executable path is user-trusted configuration; discovery does not verify product identity.
