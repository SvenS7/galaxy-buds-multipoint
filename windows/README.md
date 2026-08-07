# budsmp on Windows

## Requirements

Python 3.9 or newer ([python.org](https://www.python.org/downloads/) or
`winget install Python.Python.3.12`). Nothing else: RFCOMM comes from
`socket.AF_BTH` in the standard library, and no build step is involved.

The buds must already be paired in **Settings → Bluetooth & devices**.

## Use

```bat
cd windows
budsmp apply
budsmp read
budsmp revert
```

`budsmp.cmd` is a one-line wrapper; `py -3 -m budsmp` with `PYTHONPATH` pointed at
`..\python` does the same thing. In PowerShell, call it as `.\budsmp`.

No administrator rights are needed. The exit code is the tool's, so it composes
fine in scripts.

## Commands

| command | what it does |
|---|---|
| `apply` | Write `asVer=2`, then read the state back to confirm it landed. |
| `revert` | Write `asVer=0`, restoring stock behaviour. |
| `read` | Report the stored `asVer` and account hashes. Writes `--asver` (default 2) first, because the buds only report when a record is re-evaluated. |
| `watch` | Listen without writing anything and report what the buds push. On the firmware we tested this usually sees nothing — see [experiments.md](../docs/experiments.md#does-the-write-persist). |
| `send <hex>...` | Send raw SMEP frames and listen. |
| `scan` | List paired devices, via `Get-PnpDevice`. |
| `frame` | Print the frame `apply` would send. No Bluetooth involved. |

`budsmp --help` lists every option. Exit codes: `0` ok, `1` usage, `2` device not
found, `3` RFCOMM open failed, `4` timeout, `5` sent but could not verify.

`sdp` is Linux-only — Windows keeps its SDP cache in the registry rather than
exposing a query tool, so the channel falls back to 29, which is where Galaxy Buds
put `SPPSERVICE4`. Pass `--channel` if your model differs.

The target device is discovered: the first paired device whose name contains
"buds", overridable with `--addr` or `--name`.

## Troubleshooting

**`rfcomm open failed` on every attempt.** The buds only run their SPP server
while awake. Take them out of the case, make them the playback device in the
volume flyout, and start playing something. `budsmp` plays a quiet 19 kHz tone
through `winsound` for exactly this reason, but it can only help if the buds are
already the default output. `--no-wake` turns it off.

**`The requested address is not valid in its context` (WSAEADDRNOTAVAIL).** The
device is not paired, or the address is wrong. Run `budsmp scan`.

**`An invalid argument was supplied` (WSAEINVAL) or `AF_BTH` missing.** No
Bluetooth radio is present or enabled, or Python was installed without socket
Bluetooth support (unusual for official builds). Check that Bluetooth is on.

**`scan` shows nothing.** `Get-PnpDevice` was unavailable or blocked by execution
policy. Pass `--addr XX:XX:XX:XX:XX:XX` instead — the tool never needs the
enumeration for anything else.

**`connected=False` on a device that is connected.** `scan` reads the PnP device
node's status, which is a proxy: it says `OK` while connected and `Unknown` once
out of range. Cosmetic, and it does not affect `apply`.

**`sent but could not verify` (exit 5).** The write went out but no state NOTIFY
came back to confirm it. The buds only push one when they re-evaluate a record —
start or stop playback and run `budsmp read`.

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

There is no auto-apply on Windows yet. The macOS side ships a small agent that
writes the frame on every connect event
([macos/README.md](../macos/README.md#keeping-it-applied)); the equivalent here
would be a Task Scheduler task triggered off the Bluetooth device-connected event
log, which nobody has written. Until then, run `apply` by hand.
