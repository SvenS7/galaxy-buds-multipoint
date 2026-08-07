# budsmp on macOS

## Build

```bash
cd macos
./build.sh
```

Needs the Xcode command line tools (`xcode-select --install`) and nothing else —
no Xcode project, no package manager. Output is `build/BudsMP.app`.

Set `BUILD_UNIVERSAL=1` to attempt a fat arm64 + x86_64 binary; by default it
builds for the host architecture.

## Use

```bash
./budsmp apply     # the fix
./budsmp read      # what the buds currently have stored
./budsmp revert    # undo
```

`budsmp` is a wrapper: it builds the app if needed, launches it, and prints its
log. Its exit code is the tool's, so it composes fine in scripts.

The first run raises the macOS Bluetooth permission prompt. **Click Allow** — the
tool blocks inside IOBluetooth until you answer, and the dialog can hide behind
other windows. macOS remembers the answer, so this happens once per build.

## Why an .app bundle instead of a plain binary

Creating an `IOBluetoothDevice` starts `IOBluetoothCoreBluetoothCoordinator`,
which is gated by TCC. TCC attributes the request to the *responsible process*,
and for a binary run from a shell that is the shell's launcher — your terminal.
Terminals carry no `NSBluetoothAlwaysUsageDescription`, so TCC does not prompt:
it aborts the process with `SIGABRT`, with no explanation.

Wrapping the binary in a `.app` that declares the usage string in its own
`Info.plist`, and launching it through LaunchServices (`open`), makes the app its
own responsible process. Then the prompt appears and the grant sticks. This is
also why the tool logs to a file — as an app launched by `open`, its stderr does
not reach your terminal.

The bundle is signed ad hoc, because TCC keys a grant to the code signature and
needs something to attach it to. Note what that does *not* buy you: an ad-hoc
signature is derived from the bundle's contents, so a rebuild that changes the
binary is a different app as far as TCC is concerned and the prompt comes back.
That is fine for a one-shot command and a nuisance for the background agent —
see [the troubleshooting entry](#troubleshooting).

## Commands

| command | what it does |
|---|---|
| `apply` | Write `asVer=2`, then read the state back to confirm it landed. |
| `daemon` | Stay resident and run the `apply` write every time the buds connect. See [Keeping it applied](#keeping-it-applied). |
| `revert` | Write `asVer=0`, restoring stock behaviour. |
| `read` | Report the stored `asVer` and account hashes. Writes `--asver` (default 2) first, because the buds only report when a record is re-evaluated. |
| `watch` | Listen without writing anything and report what the buds push. On the firmware we tested this usually sees nothing — see [experiments.md](../docs/experiments.md#does-the-write-persist). |
| `send <hex>...` | Send raw SMEP frames and listen. |
| `scan` | List paired devices and their RFCOMM services. |
| `sdp` | Fresh SDP query on the target; prints the channel map. |
| `frame` | Print the frame `apply` would send. No Bluetooth, no prompt. |

`budsmp --help` lists every option. Exit codes: `0` ok, `1` usage, `2` device not
found, `3` RFCOMM open failed, `4` timeout, `5` sent but could not verify.
`daemon` is the exception — it does not exit on a failed write, it logs and waits
for the next connection.

The target device and RFCOMM channel are both discovered, not hard-coded: the
device is the first paired one whose name contains "buds" (override with `--addr`
or `--name`), and the channel comes from the device's `SPPSERVICE4` SDP record,
falling back to 29 (override with `--channel`).

## Keeping it applied

The buds forget `asVer` whenever they power down, so `apply` is a per-power-session
chore — see [asver-lifetime.md](../docs/asver-lifetime.md). Rather than remembering
it, hand it to a background agent:

```bash
./install-agent.sh
```

That writes a per-user LaunchAgent at
`~/Library/LaunchAgents/io.github.galaxy-buds-multipoint.budsmp.plist` and starts
it. It needs no sudo, adds no login item you have to click through, and does
nothing at all until a paired device whose name matches connects — at which point
it sends one frame and goes back to sleep.

```bash
./install-agent.sh status      # loaded? running? what did it log?
./install-agent.sh log         # follow the log live
./install-agent.sh uninstall   # stop it and remove the plist
```

Options are forwarded to `budsmp`, so anything you would pass to `apply` works
here too:

```bash
./install-agent.sh --name "Buds2 Pro" --debounce 15 --timeout 45
```

Its log lives at `~/Library/Logs/galaxy-buds-multipoint-daemon.log`. Unlike the
one-shot commands it appends rather than starts fresh, and stamps every line,
because for a process that runs for days the useful question is *when*. A normal
session looks like:

```
08-08 05:31:07  === budsmp daemon ===
08-08 05:31:07  daemon: writing asVer=2 whenever a paired device whose name contains "buds" connects
08-08 05:31:07  daemon: wake tone off, debounce 8.0s
08-08 05:31:07  daemon: ready
08-08 09:14:22  daemon: connected — "Galaxy Buds2 Pro" xx-xx-xx-xx-xx-xx
08-08 09:14:26    asVer (this host)  : 2   [multipoint allowed]
08-08 09:14:26    asVer as reported  : 1 -> 2
08-08 09:14:26  daemon: written and verified — asVer=2
```

The `1 -> 2` is the useful part: the first value is what the record held before the
write, so `1` there is the record having been cleared by a power cycle, exactly as
expected.

A few deliberate choices, in case the behaviour surprises you:

- **Run `./budsmp apply` by hand once before installing** — and again after every
  rebuild. The Bluetooth permission prompt has to be answered by a human, and a
  background agent is in no position to ask. The grant is keyed to the app's code
  signature, so once you have clicked Allow the agent inherits it; but `build.sh`
  produces a new ad-hoc signature whenever the binary changes, which starts the
  whole thing over. `install-agent.sh` warns you when it can't tell that the prompt
  has been answered for the current build.
- **No wake tone.** `apply` plays a quiet 19 kHz tone to keep the buds' SPP server
  up; the daemon does not, because on a connect event the buds are awake by
  definition, and a background process has no business seizing your audio output.
  Pass `--wake` if you want it anyway.
- **Repeat connect events are ignored for 8 seconds** (`--debounce`). macOS can
  report the same device connecting more than once, and there is no point writing
  the same byte twice. A *disconnect* clears the debounce, so a quick trip to the
  case still re-applies.
- **The daemon is more patient than `apply`** — at least 15 RFCOMM attempts rather
  than the default, because the buds' SPP server usually takes a moment to come up
  after a connection is established.

If you would rather not have a resident process, the alternative is just running
`./budsmp apply` after the buds come out of the case. Nothing else changes.

## Troubleshooting

**`rfcomm open failed` / `0xe00002bc` on every attempt.** The buds only run their
SPP server while awake. Take them out of the case, make them the audio output
device in the menu bar, and start playing something. `budsmp` plays a quiet
19 kHz tone for exactly this reason, but that only helps if the buds are already
the default output — the log prints which device that is, so check that line
first. `--no-wake` turns the tone off.

**One or two failed attempts then success.** Normal. The SPP4 server takes a
moment to come up; that is what the retry loop is for.

**`no paired device whose name contains "buds"`.** Run `./budsmp scan` and pass
`--addr` explicitly. Devices must be paired, not merely in range.

**`sent but could not verify` (exit 5).** The write went out but no state NOTIFY
came back to confirm it. The buds only push one when they re-evaluate a record —
start or stop playback and run `./budsmp read`.

**Nothing printed at all.** macOS blocked the app. Check
System Settings → Privacy & Security → Bluetooth and allow `BudsMP`.

**It worked, and now it doesn't.** Expected after the buds have been in the case —
they don't keep the value. Run `./budsmp apply` again, or install the agent so you
don't have to (see [Keeping it applied](#keeping-it-applied)). If you want to
confirm that is what happened, the first number in `asVer as reported` is what was
stored before the write; `1` means the record had been cleared. See
[docs/experiments.md](../docs/experiments.md#does-the-write-persist).

**The agent is loaded but nothing happens on connect.** Check
`./install-agent.sh status`. A log line ending `is not connected after all` means
macOS reported the device and then withdrew it — usually the buds were still in the
case.

**`Bluetooth access has not been granted to this build`.** The usual cause is a
rebuild. An ad-hoc code signature is computed from the bundle's contents, so a
`build.sh` that changes the binary produces a new identity, and TCC wants the
prompt answered for it — which a background agent cannot do. Run `./budsmp apply`
in a terminal, click Allow, then `./install-agent.sh` again (also needed because
the plist points at an absolute path inside `build/`). Until you do, the daemon
keeps exiting and launchd keeps restarting it every 30 seconds, logging the same
line each time.

If the daemon instead stops dead after `wake tone off` and logs nothing more, it
is stuck waiting on that same authorization check and the 20-second watchdog has
not fired yet. Give it a moment; the diagnostic above is what it will say.

## Multipoint behaviour after the fix

Both devices stay connected, and audio follows whoever is actively playing. If
your phone is playing and you start a video on the Mac, the Mac does **not** grab
the stream immediately; stop the phone and it switches over. That is ordinary
multipoint arbitration — active stream wins — and not specific to this fix.

The fix is not permanent, though. A disconnect and reconnect is fine, but once the
buds power down in the case the `asVer` byte is back to `1` and the phone starts
getting dropped again. Re-run `apply` when that happens — it takes a second and
needs no re-pairing — or let [the agent](#keeping-it-applied) do it. Why the
firmware leaves no better option is
[docs/asver-lifetime.md](../docs/asver-lifetime.md).
