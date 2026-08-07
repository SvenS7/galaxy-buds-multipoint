# budsmp on Linux

## Requirements

Python 3.9 or newer, and BlueZ — both of which a desktop Linux already has.
Nothing to build, nothing to install: RFCOMM comes from `socket.AF_BLUETOOTH` in
the standard library.

The buds must already be **paired** (`bluetoothctl` → `scan on`, `pair`, `trust`).

## Use

```bash
cd linux
./budsmp apply     # the fix
./budsmp read      # what the buds currently have stored
./budsmp revert    # undo
```

`./budsmp` is a one-line wrapper for `PYTHONPATH=../python python3 -m budsmp`, so
either form works. Its exit code is the tool's, so it composes fine in scripts.

## Commands

| command | what it does |
|---|---|
| `apply` | Write `asVer=2`, then read the state back to confirm it landed. |
| `revert` | Write `asVer=0`, restoring stock behaviour. |
| `read` | Report the stored `asVer` and account hashes. Writes `--asver` (default 2) first, because the buds only report when a record is re-evaluated. |
| `watch` | Listen without writing anything and report what the buds push. On the firmware we tested this usually sees nothing — see [experiments.md](../docs/experiments.md#does-the-write-persist). |
| `send <hex>...` | Send raw SMEP frames and listen. |
| `scan` | List paired devices. |
| `sdp` | `sdptool browse` on the target; prints the RFCOMM channel map. |
| `frame` | Print the frame `apply` would send. No Bluetooth involved. |

`./budsmp --help` lists every option. Exit codes: `0` ok, `1` usage, `2` device not
found, `3` RFCOMM open failed, `4` timeout, `5` sent but could not verify.

The target and the channel are discovered, not hard-coded: the device is the
first paired one whose name contains "buds" (override with `--addr` or `--name`),
and the channel comes from the `SPPSERVICE4` SDP record, falling back to 29
(override with `--channel`).

## Troubleshooting

**`rfcomm open failed` / `Connection refused` on every attempt.** The buds only
run their SPP server while awake. Take them out of the case, make them the audio
output device, and start playing something. `budsmp` plays a quiet 19 kHz tone
for exactly this reason, but it can only help if the buds are already the default
sink. `--no-wake` turns it off.

**`wake tone: no audio player found`.** Install any one of `paplay`
(`pulseaudio-utils`), `pw-play` (`pipewire-utils`), `aplay` (`alsa-utils`), or
`ffplay`. Or ignore it and start playback by hand — the tone is a convenience.

**`could not read SDP records (sdptool missing …)`.** Harmless: the tool falls
back to channel 29, which is where Galaxy Buds put `SPPSERVICE4`. `sdptool` lives
in BlueZ's deprecated tools and many distros no longer ship it. If your model
differs, pass `--channel`.

**`could not enumerate paired devices`.** `bluetoothctl` is missing or refused
the query. Pass `--addr XX:XX:XX:XX:XX:XX` instead.

**`Permission denied` opening the socket.** Add yourself to the `bluetooth`
group and log back in. Most distributions do not require this for an outbound
RFCOMM connection to a paired device.

**`this Python has no Bluetooth socket support`.** A Python built without the
BlueZ headers. Use the distribution's `python3` rather than a hand-built one.

**`sent but could not verify` (exit 5).** The write went out but no state NOTIFY
came back to confirm it. The buds only push one when they re-evaluate a record —
start or stop playback and run `./budsmp read`.

**It worked, and now it doesn't.** Expected after the buds have been in the case —
they don't keep the value. Run `budsmp apply` again. The first number in `asVer as
reported` is what was stored before the write; `1` means the record had been
cleared. See [experiments.md](../docs/experiments.md#does-the-write-persist).

## Multipoint behaviour after the fix

Both devices stay connected, and audio follows whoever is actively playing. If
your phone is playing and you start a video on the PC, the PC does **not** grab
the stream immediately; stop the phone and it switches over. That is ordinary
multipoint arbitration — active stream wins — and not specific to this fix.

The fix is not permanent, though. A disconnect and reconnect is fine, but once the
buds power down in the case the `asVer` byte is back to `1` and the phone starts
getting dropped again. Re-run `apply` when that happens — it takes a second and
needs no re-pairing. Why the firmware leaves no better option is
[docs/asver-lifetime.md](../docs/asver-lifetime.md).

There is no auto-apply on Linux yet. The macOS side ships a small agent that writes
the frame on every connect event ([macos/README.md](../macos/README.md#keeping-it-applied));
the equivalent here would be a systemd user unit triggered off a BlueZ
`InterfacesAdded`/`Connected` signal, which nobody has written. If you do, a PR
would be welcome. In the meantime a `udev` rule or a shell alias is usually enough.

BlueZ will not switch profiles for you: if the buds come up as HSP/HFP rather
than A2DP, pick `a2dp-sink` in your sound settings. Unrelated to this fix, but it
is the usual reason audio sounds wrong afterwards.
