# galaxy-buds-multipoint

Galaxy Buds multipoint on your computer — no Samsung account needed.

> **Tested on Galaxy Buds3 Pro**

> **This fork adds — Windows Tray App by SvenS7** on top of the original by **id6917824**: tray-first `tray/app.py` (`uv run app.py` → system tray, no CLI), robust auto-fix on every connect/boot/case power-cycle (polling + hysteresis + RFCOMM lock, `tray/buds_fix.py`), reliable autorun at **boot + login** (Task Scheduler XML `LogonTrigger+BootTrigger` + fallbacks, `tray/startup.py`), full uninstall, `uv` project and English UI. Original `python/budsmp/*` vendored unchanged. Based on <https://github.com/id6917824/galaxy-buds-multipoint> (MIT). See [What's new in this fork](#whats-new-in-this-fork-windows-tray-app-by-svens7) and [tray/README.md](tray/README.md).

*[한국어](README.ko.md)*

You probably know the moment. Your Galaxy Buds2 Pro are connected to your phone
and your laptop at the same time, which is the entire point of multipoint, and
then you press play on the laptop and the buds quietly hang up on your phone.

Ask around and you'll get the same answer everywhere: Samsung gates multipoint
behind "both devices signed in to the same Samsung account", and a computer
simply cannot satisfy that. It sounds convincing. It's also the wrong diagnosis.

The firmware does look at your account — but only after it checks something else
first, and a computer trips on *that* one, well before the account ever comes up.
What it checks is a single version byte, set by one protocol frame that a Galaxy
phone sends while pairing and that no third-party client sends. Send it once and
multipoint behaves: phone and computer connected together, account never
consulted.

```bash
cd macos && ./budsmp apply
```

That's the whole thing — one frame, `fc0b00014304030400000b021eafcc`. It sticks
across disconnects and reconnects, but not across the buds powering themselves
down: the byte lives in RAM they wipe at boot, so once they've been sitting in the
case, multipoint misbehaves again and you run `apply` again. Annoying, and not what
we expected — how it was measured and why the firmware leaves no way around it are
in [docs/experiments.md](docs/experiments.md#does-the-write-persist). If you'd
rather undo it entirely, `./budsmp revert` puts everything back exactly as it was.

On macOS you don't have to remember any of that: `macos/install-agent.sh` sets up
a small background agent that writes the byte for you every time the buds connect.
See [Keeping it applied](macos/README.md#keeping-it-applied).

On Windows you now also don't have to remember it: `tray/` runs in the system tray and reapplies the fix on every connect, boot and case power-cycle. See [tray/README.md](tray/README.md) and [What's new in this fork](#whats-new-in-this-fork-windows-tray-app-by-svens7) below.

## Where things stand

| platform | state | how |
|---|---|---|
| macOS 11+ | working, verified on hardware | Swift + IOBluetooth, in [macos/](macos/) |
| Linux | written, not yet run against buds | Python + `AF_BLUETOOTH`, in [linux/](linux/) |
| Windows 10+ | working, verified on hardware (Buds3 Pro, `tray/` app) | Python + `AF_BTH`, in [windows/](windows/) and [tray/](tray/) |
| Windows Tray | working, verified on hardware (Buds3 Pro) | Python + `pystray` + `tkinter`, in [tray/](tray/) — `uv run app.py` |

That middle column means exactly what it says. The Linux and Windows tools build
byte-for-byte the same frames as the macOS one, checked against frames captured
from real hardware, and their socket, discovery and reporting paths have tests of
their own — but nobody has actually pointed them at a pair of buds yet. If you
get there first, it would be great to hear how it went.

All of this was developed and confirmed on Galaxy Buds2 Pro. The mechanism isn't
model-specific, so other Galaxy Buds that advertise `SPPSERVICE4` should work the
same way, though that's untested — reports very welcome.

## What's actually going on

When audio comes up on a second device, the buds run a two-stage check.

**Stage one — the `asVer` gate.** The buds keep a small record for every peer
they're paired with, and one byte of it is `asVer`. If either peer's value lands
outside `{2, 3}`, the buds disconnect one of them with reason `0xa9`. This is the
stage a computer never gets past.

**Stage two — the account gate.** Only reached once stage one passes. It compares
the two peers' Samsung account hashes, and skips the comparison altogether when
one peer is classified "special" — which a computer is.

So the account check everyone blames was never really the obstacle. Your computer
wasn't failing it; it was never reaching it.

Exactly one message writes `asVer`: `MDE_VERSION`, on the `SPPSERVICE4` RFCOMM
channel. A Galaxy phone sends it during pairing. Third-party clients such as
[GalaxyBudsClient](https://github.com/timschneeb/GalaxyBudsClient) talk to a
*different* channel (`GEARMANAGER`) and never send it, so their `asVer` stays at
`0` and stage one keeps catching them.

Which makes the fix pleasantly boring: send that one message. Its handler
validates nothing beyond `version <= 3` — no signature, no nonce, nothing
account-shaped.

And that the account hash genuinely doesn't matter isn't an assumption. It was
tested on hardware, with a positive control to show the gate was live: a
deliberately wrong account left the phone connected, a zeroed account left it
connected, and `asVer = 0` with the *correct* account dropped it. Method and
results are in [docs/experiments.md](docs/experiments.md).

If you want the full picture, [docs/protocol.md](docs/protocol.md) covers the
wire format, [docs/firmware-gate.md](docs/firmware-gate.md) covers the gate on
the firmware side, and [docs/asver-lifetime.md](docs/asver-lifetime.md) follows
that one byte around the firmware — everything that can write it, everything that
erases it, and why no host can make it stick.

## Getting it running

### macOS

```bash
git clone https://github.com/id6917824/galaxy-buds-multipoint
cd galaxy-buds-multipoint/macos
./build.sh
./budsmp apply
```

You'll need the Xcode command line tools (`xcode-select --install`), and nothing
else. Connect the buds first, and when macOS asks for Bluetooth access, **click
Allow** — the tool sits and waits for that answer, and the dialog has a habit of
hiding behind other windows.

```bash
./budsmp read      # what the buds have stored for this host
./budsmp revert    # undo
./budsmp --help    # every command and option
```

If `apply` can't open the channel, the buds are asleep. Take them out of the
case, make them your audio output, play something, and try again.
[macos/README.md](macos/README.md) walks through the other failure modes.

Once it works, this makes it stick:

```bash
./install-agent.sh
```

A per-user LaunchAgent, no sudo, no login item — it watches for the buds
connecting and writes the frame each time. `./install-agent.sh uninstall` removes
it again.

### Linux

```bash
git clone https://github.com/id6917824/galaxy-buds-multipoint
cd galaxy-buds-multipoint/linux
./budsmp apply
```

Needs Python 3.9+ and BlueZ, which a desktop Linux almost certainly already has.
There's nothing to build — RFCOMM comes straight from the standard library. Pair
the buds first with `bluetoothctl`, then have a look at
[linux/README.md](linux/README.md).

### Windows

```bat
git clone https://github.com/id6917824/galaxy-buds-multipoint
cd galaxy-buds-multipoint\windows
budsmp apply
```

Needs Python 3.9+ and nothing else — no administrator rights, no build step. Pair
the buds in Settings first, then see [windows/README.md](windows/README.md).

### Windows Tray App (added in this fork — SvenS7)

No CLI needed — fully tray-driven. See [tray/README.md](tray/README.md).

```powershell
cd tray
uv sync
uv run app.py                 # blue B icon appears in the system tray
```

Right-click the tray: `Open` / `Check Buds status` / `Run fix now` / `Revert fix` / `Enable Autorun` / `Disable Autorun` / `Uninstall (full)` / `Close`. The app polls every 3s, reapplies on every `disconnected → connected` (20s debounce), does an immediate startup check at boot/login and a re-verify every 90s (auto re-apply after case/power-cycle when `asVer` falls back to `1`), and serializes RFCOMM access to avoid `10048` collisions. Autorun uses Task Scheduler with **both** `LogonTrigger` and `BootTrigger` (single XML task) with fallbacks to Startup `.bat` and `HKCU\...\Run`. See [What's new in this fork](#whats-new-in-this-fork-windows-tray-app-by-svens7) for full details.

### Just the protocol tools

The frame builder and decoder run anywhere Python 3 does, with no dependencies:

```bash
cd python
python3 -m budsmp.frame version-only 2                         # build the fix frame
python3 -m budsmp.frame decode fc0b00014304030400000b021eafcc   # take it apart
python3 -m budsmp.frame selftest                               # check against captured bytes
```

## Is this safe to run?

A fair thing to ask before letting a stranger's code near your headphones. What
it does is write one byte-sized field in a per-device record the buds already
keep for your computer. Which means:

- **It's reversible.** `./budsmp revert` restores the original value, and you
  won't have to re-pair anything.
- **No firmware modification.** Nothing is flashed. This is an ordinary protocol
  message, sent over a channel the buds themselves advertise, and they accept it
  the same way they'd accept it from a phone.
- **Nothing leaves your machine.** No network, no account, no telemetry. The only
  thing talking to your buds is your own computer, over Bluetooth.
- **Only your host is touched.** The record for the computer you run it on, and
  nothing else — your phone's record is left alone.
- **Nothing here is permanent.** The buds clear the value on their own when they
  power down, and a factory reset clears it too. Either way you just run it again.

What it doesn't do is equally clear: it won't unlock features your buds don't
have, it doesn't touch audio processing, and it changes nothing on your phone.

Realistically the worst that happens is the buds briefly drop one device — which
is the thing they were already doing before the fix, and which the next write
undoes.

## Limitations

- **It doesn't stay applied.** Reconnecting is fine, but after the buds power down
  in the case you'll need to run `apply` again. There's no host-side way around
  that: the records live in RAM the buds clear at boot, and the routine that
  restores a record when you reconnect deliberately leaves this one byte alone —
  it restores your account hash but writes `asVer` back over itself. Only a
  firmware change could pin it. Measurement and addresses:
  [docs/experiments.md](docs/experiments.md#does-the-write-persist).
- Only the host you run it on gets fixed, so a second computer needs its own run.
- The buds hold two active links, and this doesn't raise that ceiling.
- Audio follows whichever device is actively playing. Starting playback on the
  idle one won't steal the stream until the other stops. That's ordinary
  multipoint arbitration rather than a defect.
- A buds factory reset clears it.

## What's new in this fork (Windows Tray App by SvenS7)

> **Original project:** <https://github.com/id6917824/galaxy-buds-multipoint> (MIT, © 2026 galaxy-buds-multipoint contributors) by **id6917824** — protocol, firmware gate, `asVer` lifetime, `python/budsmp/*` frame/transport/discovery (vendored unchanged in `tray/budsmp/`).

This fork adds a **Windows-only tray extension** on top of that core:

- **Tray-first UX** (`tray/app.py`, `tray/tray_app.py`) — `uv run app.py` straight to the system tray (`pystray` + `Pillow` + `tkinter`), no CLI needed. Menu: `Open` / `Check Buds status` / `Run fix now` (`asVer=2`) / `Revert fix` / `Enable Autorun` / `Disable Autorun` / `Uninstall (full)` / `Close`; single-instance lock, hidden console when `--minimized`, dynamic enable/disable.
- **Robust monitor** (`tray/buds_fix.py` — `BudsMonitor`) — polls `Get-PnpDevice` every 3s with 20s debounce, immediate startup status-check + auto-`apply` if already paired/connected, extra RFCOMM reachability probe when PnP says disconnected, 2-poll hysteresis for PnP flapping, periodic re-verify every 90s (auto re-apply when `asVer` is back to `1` after case/power-cycle), global `_rfcomm_lock` to prevent `10048` collisions. Reuses `frame.version_only(2)` → `fc0b00014304030400000b021eafcc`.
- **Reliable autorun** (`tray/startup.py`) — prefers Task Scheduler XML with **both** `LogonTrigger` **and** `BootTrigger` (`GalaxyBudsMultipointFix`) so the app runs at boot *and* login; fallbacks: separate `onlogon` + `onstart` (`GalaxyBudsMultipointFixBoot`) → Startup `.bat` → `HKCU\...\Run`. `Disable Autorun` / `Uninstall` removes all variants.
- **Full uninstall** — removes all Scheduler tasks, Startup items, registry key, `%LOCALAPPDATA%\GalaxyBudsMultipoint\logs`, `tray/logs/*.log`, `%TEMP%\budsmp-wake-*.wav`, lock file and AppData config, with a dialog of what was removed.
- **Tooling & i18n** — `tray/pyproject.toml` (`pystray`, `Pillow`, `requires-python >=3.9`, `uv` managed, `uv.lock`), `tray/config.json` (`poll_interval`, `debounce_seconds`, `verify_interval`, …), all UI/logs translated to English, quiet operation (15s light refresh, heavy RFCOMM only on explicit action, rotating logs).
- **Verified** on Buds3 Pro (`2C:DA:46:9D:33:94`) — `uv run app.py --status` reports `asVer=2` / `multipoint allowed`; tray runs silently without the previous `10048` storm.

See `tray/README.md` for full usage, autorun details, reconnect/case handling and safety notes. The original `python/budsmp/*`, `windows/`, `linux/`, `macos/` and `docs/` remain as in the upstream.

## Disclaimer

This is reverse engineering of hardware the author owns, done for
interoperability with it — documenting one protocol detail so that devices
already sitting in someone's pocket work together properly. It ships no Samsung
code. The tray extension by **SvenS7** is also MIT and reuses the original logic unchanged.

Please use it on your own devices. It isn't affiliated with, endorsed by, or
supported by Samsung, it may well void your warranty, and a firmware update could
stop it working at any time. It comes as is, with no warranty of any kind — see
[LICENSE](LICENSE).

"Galaxy Buds" and "Samsung" are trademarks of Samsung Electronics.

## Contributing

Things that would genuinely help:

- **Did the Linux or Windows tool work for you?** Both are written but unproven
  against real buds, so a simple "worked" or "failed, here's the log" is the most
  valuable thing anyone can send right now.
- **Other Galaxy Buds models.** Does `budsmp scan` show `SPPSERVICE4`, and does
  `apply` do the trick? Please include the model and firmware version.

One request: scrub your logs before posting them. `budsmp` output contains your
Bluetooth addresses, and `read` prints your Samsung account hash.
