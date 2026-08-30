# Galaxy Buds Multipoint Fix — Windows Tray App

> **Tested on Galaxy Buds3 Pro**

Compact Windows Python app that automatically applies `budsmp apply` (`asVer=2` via `SPPSERVICE4` RFCOMM) whenever your paired Galaxy Buds connect. **Fully tray-driven — no CLI needed.**

> **Releases:** `BudsFix.exe` (onefile, single exe) and `BudsFix-standalone.zip` (folder with exe + DLLs). Note: antivirus may flag the onefile as virus because it is packaged with Nuitka onefile — if that happens, use the standalone zip instead.

- Double-click `app.py` / `uv run app.py` / later `BudsFix.exe` → blue **B** icon in the system tray
- Right-click the tray for everything (status, fix, autorun, uninstall)
- Optional auto-start at login/boot (Task Scheduler)
- Small Tkinter status window with live info
- 100% local, no network/telemetry, logs only in `%LOCALAPPDATA%`

---

## What's new in this tray fork (added on top of the original project)

This `tray/` app is a **Windows-only extension** built on the original `galaxy-buds-multipoint` core. The original `python/budsmp/*` is vendored **unchanged** in `tray/budsmp/`. What's added:

- **Tray-first entrypoint** (`tray/app.py`) — `uv run app.py` goes straight to the tray; all control via right-click. Single-instance lock (`%TEMP%\GalaxyBudsMultipointFix.lock`), hidden console when `--minimized`, quiet tray (no spam popups).
- **System tray + status window** (`tray/tray_app.py` — `pystray` + `Pillow` + `tkinter`) — menu: `Open` (default) / `Check Buds status` / `Run fix now` (`asVer=2`) / `Revert fix` (`asVer=0`) / `Enable Autorun` / `Disable Autorun` (enabled/disabled dynamically) / `Uninstall (full)` / `Close`. Status window shows paired/connected/fix/autorun + `Check`/`Run`/`Revert`/`Enable`/`Disable` buttons and a rolling log.
- **Robust detection & auto-fix** (`tray/buds_fix.py` — `BudsMonitor` thread) — polling `Get-PnpDevice` with debounce, plus:
  - Immediate status check at startup and auto-`apply` if Buds are already paired/connected.
  - Extra RFCOMM reachability probe when PnP says `disconnected` but the Buds are actually awake (ground truth via short RFCOMM open), with `2-poll` hysteresis to smooth PnP flapping.
  - Periodic re-verify every 90s while connected; if `asVer` fell back to `1` after a case/power-cycle, it re-applies automatically without user action. Debounce is bypassed for `startup` and `asVer=1 → re-apply`.
  - Global `_rfcomm_lock` serializes all RFCOMM access (monitor + UI) to prevent `WSAEADDRINUSE (10048)` collisions.
  - Fully local, no network, reuses original `frame.version_only(2)` → `fc0b00014304030400000b021eafcc`.
- **Reliable autorun** (`tray/startup.py`) — prefers Task Scheduler with **both** `LogonTrigger` **and** `BootTrigger` in one XML task (`GalaxyBudsMultipointFix`), so the app runs after boot _and_ after login. Fallback chain if Scheduler is blocked: separate `onlogon` + `onstart` (`GalaxyBudsMultipointFixBoot`) tasks → Startup folder `.bat` → `HKCU\...\Run` registry. `Disable Autorun` / `Uninstall` cleanly removes all variants. Verified on Buds3 Pro (`2C:DA:46:9D:33:94`).
- **Clean uninstall** — `tray/startup.py:uninstall_all()` removes _all_ Scheduler tasks (logon + boot), Startup `.bat`/`.lnk`, registry key, `%LOCALAPPDATA%\GalaxyBudsMultipoint\logs`, `tray/logs/*.log`, `%TEMP%\budsmp-wake-*.wav`, lock file and AppData config, then shows a dialog of what was removed. `Close` only stops the tray (autorun stays).
- **Project tooling** — `tray/pyproject.toml` (`pystray` + `Pillow`, `requires-python >=3.9`), `uv` managed (`uv sync` / `uv run app.py` / `uv.lock`), `tray/config.json` with `poll_interval`, `debounce_seconds`, `verify_interval`, `disconnected_retry_seconds`, etc.
- **Quiet operation** — console is hidden when `--minimized` (Task/exe), status window auto-refresh is light (15s, only `Autorun: on/off`), heavy RFCOMM `check_status` only on explicit user action; logs go to rotating files in AppData and `tray/logs/`.

In short: original project proved and documented the one-byte fix; this fork makes it **stick on Windows without manual `apply`** — tray-driven, boot-resilient, case-cycle-proof.

---

## Usage — tray only (no CLI)

```powershell
cd tray
uv sync
uv run app.py                 # shows tray icon immediately (no console needed)
# without uv:
pip install pystray Pillow
python app.py                 # or pythonw app.py for no console
# later as exe:
BudsFix.exe                   # same — tray appears
```

Right-click the blue **B** icon:

| Menu item             | What it does                                                 |
| --------------------- | ------------------------------------------------------------ |
| **Open**              | Show status window (paired / connected / fix / autorun)      |
| **Check Buds status** | Read `asVer` from Buds via RFCOMM and show multipoint status |
| **Run fix now**       | Apply fix immediately (`asVer=2`)                            |
| **Revert fix**        | Restore stock behavior (`asVer=0` → `1`)                     |
| **Enable Autorun**    | Create Task Scheduler task(s) for boot + login               |
| **Disable Autorun**   | Remove task / Startup item / Registry key                    |
| **Uninstall (full)**  | Remove autorun + logs + temp files, show what was removed    |
| **Close**             | Quit tray + monitor (autorun stays)                          |

Menu items `Enable`/`Disable` are automatically enabled/disabled based on current autorun state. The status window (Open) has the same actions as buttons.

---

## Autorun details

Tray → **Enable Autorun** creates (preferred) a single Task Scheduler task with both triggers:

```
GalaxyBudsMultipointFix  LogonTrigger + BootTrigger  /RL limited  InteractiveToken
  -> "pythonw.exe" "…\tray\app.py" --minimized
```

Check manually:

```powershell
schtasks /query /tn GalaxyBudsMultipointFix /v
schtasks /query /tn GalaxyBudsMultipointFixBoot /v   # fallback boot task
dir "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\Galaxy Buds*"
reg query HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v GalaxyBudsMultipointFix
```

Fallbacks if Task Scheduler is blocked (`Access denied`): Startup folder `.bat` and `HKCU\...\Run` registry. **Disable Autorun** removes all three. **Close** leaves autorun intact — tray returns after next login/boot.

---

## Reconnect / case / power-cycle

`asVer` lives in Buds RAM and is cleared on power-down in the case (`docs/asver-lifetime.md`). The monitor polls `Get-PnpDevice` every 3s; on `disconnected → connected` it re-applies (20s debounce). `disconnected` also clears debounce so a case trip always re-applies. While connected, a re-verify every 90s reads `asVer` back; if it's back to `1` (power-cycle), it re-applies automatically. You notice nothing.

---

## Safety

- Only RFCOMM write `version_only(2)` to `SPPSERVICE4` ch29, no other Bluetooth action.
- Reversible via Tray → Revert (`asVer=0`).
- Standard Python + `pystray`/`Pillow`; `tkinter` is stdlib; 100% local, no telemetry.

---

## Advanced (optional CLI)

CLI is not needed for normal use, but remains for debugging:

```powershell
uv run app.py --minimized   # hidden start (for Task/exe)
uv run app.py --verbose     # debug logging
uv run app.py --status      # one-shot status in console (hidden flag)
```

---

## Credits & origin

- **Original project & research:** **id6917824 / galaxy-buds-multipoint** — <https://github.com/id6917824/galaxy-buds-multipoint> (MIT, © 2026 galaxy-buds-multipoint contributors). Protocol, firmware gate and `asVer` lifetime documented in `docs/protocol.md`, `docs/firmware-gate.md`, `docs/asver-lifetime.md` and `docs/experiments.md` — including the finding that `MDE_VERSION` on `SPPSERVICE4` ch29 with `asVer=2` (`fc0b00014304030400000b021eafcc`) is the only host-side fix. Frame building, RFCOMM transport and discovery in `python/budsmp/{frame,transport,discover,wake,cli}.py` are vendored unchanged in `tray/budsmp/`; clean-room implementation without Samsung code, without GalaxyBudsClient code (MPL-2.0).
- **Windows tray app (this `tray/` extension):** **SvenS7** — `tray/app.py`, `tray/tray_app.py`, `tray/buds_fix.py`, `tray/startup.py`, `tray/config.json`, `tray/pyproject.toml`, robust autorun (logon + boot), polling with hysteresis, RFCOMM serialization, case/power-cycle re-apply, quiet tray and full uninstall. Built on top of the original fix without modifying its logic.

See `LICENSE` (MIT) and the original `README.md` disclaimer — not affiliated with Samsung, no warranty.
