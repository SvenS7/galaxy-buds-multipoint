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

The bundle is signed ad hoc, because TCC keys a grant to the code signature; an
unsigned bundle would re-prompt on every rebuild.

## Commands

| command | what it does |
|---|---|
| `apply` | Write `asVer=2`, then read the state back to confirm it landed. |
| `revert` | Write `asVer=0`, restoring stock behaviour. |
| `read` | Report the stored `asVer` and account hashes. |
| `send <hex>...` | Send raw SMEP frames and listen. |
| `scan` | List paired devices and their RFCOMM services. |
| `sdp` | Fresh SDP query on the target; prints the channel map. |
| `frame` | Print the frame `apply` would send. No Bluetooth, no prompt. |

`budsmp --help` lists every option. Exit codes: `0` ok, `1` usage, `2` device not
found, `3` RFCOMM open failed, `4` timeout, `5` sent but could not verify.

The target device and RFCOMM channel are both discovered, not hard-coded: the
device is the first paired one whose name contains "buds" (override with `--addr`
or `--name`), and the channel comes from the device's `SPPSERVICE4` SDP record,
falling back to 29 (override with `--channel`).

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

## Multipoint behaviour after the fix

Both devices stay connected, and audio follows whoever is actively playing. If
your phone is playing and you start a video on the Mac, the Mac does **not** grab
the stream immediately; stop the phone and it switches over. That is ordinary
multipoint arbitration — active stream wins — and not specific to this fix.
