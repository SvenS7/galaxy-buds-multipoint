# galaxy-buds-multipoint

Enable Galaxy Buds multipoint on a computer, with no Samsung account.

*[한국어](README.ko.md)*

Galaxy Buds2 Pro support multipoint, but they hang up on your phone the moment a
non-Galaxy device starts playing audio. The usual explanation is that Samsung
gates multipoint behind "both devices on the same Samsung account", and that a
computer cannot satisfy it.

That turns out to be the wrong diagnosis. The firmware checks a version byte
first, and a computer fails on *that*, never reaching the account check at all.
The byte is set by a single protocol frame that a Galaxy phone sends at pairing
and that no third-party client sends. Send it once and multipoint works — phone
and computer connected at the same time, no account involved.

```bash
cd macos && ./budsmp apply
```

One frame, `fc0b00014304030400000b021eafcc`, written once. It persists in the
buds' own storage, so reconnects and reboots keep working, and `./budsmp revert`
puts it back exactly as it was.

## Status

| platform | state | how |
|---|---|---|
| macOS 11+ | working, verified on hardware | Swift + IOBluetooth, in [macos/](macos/) |
| Linux | implemented, not yet run against buds | Python + `AF_BLUETOOTH`, in [linux/](linux/) |
| Windows 10+ | implemented, not yet run against buds | Python + `AF_BTH`, in [windows/](windows/) |

"Not yet run against buds" is meant literally. The Linux and Windows tools build
byte-for-byte the same frames as the macOS one, checked against frames captured
from real hardware, and their socket, discovery and reporting paths have their own
tests — but nobody has yet pointed them at actual buds. If you try it, please say
whether it worked.

Developed and confirmed on Galaxy Buds2 Pro. The mechanism is not model-specific
and should apply to other Galaxy Buds that advertise `SPPSERVICE4`, but that is
untested — reports welcome.

## How it works

When audio comes up on a second device, the buds run a two-stage check:

1. **`asVer` gate.** Every paired peer has an `asVer` byte in the buds' storage.
   If either peer's value is outside `{2, 3}`, the buds disconnect one of them
   (reason `0xa9`).
2. **Account gate.** Only reached if stage 1 passed. Compares the two peers'
   Samsung account hashes — and skips the comparison entirely when one peer is
   classified "special", which a computer is.

`asVer` is written by exactly one message, `MDE_VERSION`, on the `SPPSERVICE4`
RFCOMM channel. A Galaxy phone sends it while pairing. Third-party clients such as
[GalaxyBudsClient](https://github.com/timschneeb/GalaxyBudsClient) connect to a
*different* channel (`GEARMANAGER`) and never send it, so their `asVer` stays `0`
and they trip stage 1 forever.

So the fix is to send that one message. The handler validates nothing beyond
`version <= 3` — no signature, no nonce, no account material.

The account hash genuinely is not needed, and that is not an assumption. It was
tested on hardware, including a positive control to prove the gate was live: a
deliberately wrong account left the phone connected, a zeroed account left the
phone connected, and `asVer = 0` with the *correct* account dropped it. Method and
results in [docs/experiments.md](docs/experiments.md).

Full detail: [docs/protocol.md](docs/protocol.md) for the wire format,
[docs/firmware-gate.md](docs/firmware-gate.md) for the firmware side.

## Install and use

### macOS

```bash
git clone https://github.com/id6917824/galaxy-buds-multipoint
cd galaxy-buds-multipoint/macos
./build.sh
./budsmp apply
```

Requires the Xcode command line tools (`xcode-select --install`) and nothing else.
Connect the buds first, and **click Allow** when macOS asks for Bluetooth access —
the tool waits for that answer, and the dialog likes to hide behind other windows.

```bash
./budsmp read      # what the buds have stored for this host
./budsmp revert    # undo
./budsmp --help    # every command and option
```

If `apply` cannot open the channel, the buds are asleep: take them out of the
case, select them as the audio output device, start playing something, and retry.
See [macos/README.md](macos/README.md) for the rest of the failure modes.

### Linux

```bash
git clone https://github.com/id6917824/galaxy-buds-multipoint
cd galaxy-buds-multipoint/linux
./budsmp apply
```

Needs Python 3.9+ and BlueZ, both of which a desktop Linux already has. Nothing
to build — RFCOMM comes from the standard library. Pair the buds first
(`bluetoothctl`), then see [linux/README.md](linux/README.md).

### Windows

```bat
git clone https://github.com/id6917824/galaxy-buds-multipoint
cd galaxy-buds-multipoint\windows
budsmp apply
```

Needs Python 3.9+ and nothing else — no administrator rights, no build step. Pair
the buds in Settings first, then see [windows/README.md](windows/README.md).

### Protocol tools

The frame builder and decoder work anywhere Python 3 runs, with no dependencies:

```bash
cd python
python3 -m budsmp.frame version-only 2                         # build the fix frame
python3 -m budsmp.frame decode fc0b00014304030400000b021eafcc   # take it apart
python3 -m budsmp.frame selftest                               # check against captured bytes
```

## Is this safe?

It writes one byte-sized field in a per-device record the buds already keep for
your computer. Specifically:

- **Reversible.** `./budsmp revert` restores the original value. No re-pairing.
- **No firmware modification.** Nothing is flashed. This is a normal protocol
  message that the buds accept over a channel they advertise.
- **Nothing sent anywhere.** No network, no account, no telemetry. The only thing
  that talks to the buds is your own machine, over Bluetooth.
- **Scoped to your host.** Only the record for the computer you run it on is
  touched. Your phone's record is not modified.
- **Does not survive a factory reset** of the buds. If you reset them, run it
  again.

What it does not do: it does not unlock features your buds do not have, does not
alter audio processing, and does not change anything on your phone.

The worst realistic failure is that the buds temporarily drop one device, which is
the same thing they were doing before the fix, and which the next write undoes.

## Limitations

- Only the host you run it on gets fixed. A second computer needs its own run.
- The buds hold two active links; this does not raise that limit.
- Audio follows the actively playing device. Starting playback on the idle device
  does not steal the stream until the other one stops. Normal multipoint
  arbitration, not a defect.
- A buds factory reset clears it.

## Disclaimer

This is the result of reverse engineering hardware the author owns, for
interoperability with it — documenting a protocol detail so that devices already
in someone's possession work together. It ships no Samsung code.

Use it on your own devices. It is not affiliated with, endorsed by, or supported
by Samsung, and it may well void your warranty or stop working after a firmware
update. Provided as is, with no warranty — see [LICENSE](LICENSE).

"Galaxy Buds" and "Samsung" are trademarks of Samsung Electronics.

## Contributing

Useful things to report:

- **Did the Linux or Windows tool work?** Both are written but unproven against
  real buds; a "worked" or "failed with this log" is the most useful thing anyone
  can send right now.
- Other Galaxy Buds models: does `budsmp scan` show `SPPSERVICE4`, and does
  `apply` work? Include the model and firmware version.

When posting logs, scrub them first. `budsmp` output contains your Bluetooth
addresses, and `read` prints your Samsung account hash.
