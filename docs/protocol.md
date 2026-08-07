# The wire protocol

Everything here was recovered from a Bluetooth HCI capture of a Galaxy phone
pairing with a pair of Galaxy Buds2 Pro, cross-checked against the buds'
firmware, and then confirmed against a live device. Byte offsets are given so
you can re-derive them yourself.

## Where MDE_VERSION lives

The buds expose a pile of RFCOMM services. A typical Buds2 Pro SDP map:

```
rfcomm 30  MULTIPOINT
rfcomm 29  SPPSERVICE4      <-- SMEP / MDE_VERSION lives here
rfcomm 28  SAMSUNGDEVICE
rfcomm 27  GEARMANAGER      <-- GalaxyBudsClient and Galaxy Wearable use this
rfcomm 26  UUID128
rfcomm 22  DEVICEID
rfcomm 21  BTIS
rfcomm 20  FACTORY
```

`SPPSERVICE4` is a *different* channel from the one third-party clients such as
[GalaxyBudsClient](https://github.com/timschneeb/GalaxyBudsClient) connect to,
which is why no existing client sends this frame — it never opens the channel.
Sending `MDE_VERSION` therefore needs its own RFCOMM connection.

The channel number is not guaranteed to be 29 on every model or firmware. Do not
hard-code it; read it from SDP. `budsmp sdp` prints the map for your device, and
`budsmp apply` resolves `SPPSERVICE4` from SDP automatically, falling back to 29.

The service's 128-bit UUID, from the firmware string `SPP4_MobileSettings`, is
`f8620674-a1ed-41ab-a8b9-de9ad655729d`.

## SMEP framing

Samsung's accessory protocol on this channel frames every message as:

```
FC | hdr1 hdr2 | 01 | msgID | payload... | CRC16 (2, LE) | CC
```

| field | meaning |
|---|---|
| `FC` | start of message |
| `hdr1 hdr2` | little-endian 16-bit word, call it `i2` |
| `01` | fixed byte at offset 3 |
| `msgID` | `0x43` SET (write), `0x44` GET (read), `0x45` NOTIFY (pushed by buds) |
| `CRC16` | CRC16-CCITT/XMODEM, little-endian |
| `CC` | end of message |

`i2 = (hdr2 << 8) | hdr1`, and:

- bits 0–9 — length of the region beginning at byte 3, i.e.
  `fixed(1) + msgID(1) + payload + CRC(2)`. Total frame size is that plus 4.
- bit 12 — response flag
- bit 13 — fragment flag
- bits 14–15 — sequence number

The CRC covers `packet[3 .. len-3]` — from the fixed `01` up to but not including
the CRC itself. Poly `0x1021`, init `0x0000`, no final XOR, no reflection.

Note that the sequence bits live in `packet[2]`, which is **outside** the CRC
range. Changing the sequence number does not change the checksum, which is why a
frame captured as `seq=1` can be replayed as `seq=0` with the same CRC.

## MDE_VERSION (opcode 0x0b)

A SET whose payload is a single TLV: tag `0x04`, type `0x03` (byte blob), then a
length and the blob.

The blob has two forms. The short one carries only the version:

```
04 03 04 | 00 00 | 0b | 02
tag ty len  prefix  op   version
```

and the long one also overwrites the stored account hash:

```
04 03 07 | 00 00 | 0b | 02 | 01 | AA BB
tag ty len  prefix  op   ver  sel  hash
```

- `00 00` — outer MDE header, consumed before the opcode is dispatched.
- `0b` — `MDE_VERSION`.
- version byte — written verbatim into the peer record's `asVer` field
  (`record[0x4f]`). The handler validates nothing but `version <= 3`. No crypto,
  no nonce, no signature.
- selector (`record[0x51]`) and hash (`record[0x52]`) — the Samsung-account
  identity, as a **2-byte** value. Not needed for multipoint; see
  [experiments.md](experiments.md).

Fully assembled, the version-only `asVer=2` frame — the whole fix — is:

```
fc0b00014304030400000b021eafcc
```

`budsmp frame --asver 2` prints it, and `tools/mkframe.py version-only 2` builds
it from scratch if you want to check the arithmetic yourself.

For reference, a phone at pairing time sends the long form twice back to back,
`version=0` then `version=2`:

```
fc0e00014304030700000b0001AABBxxxxcc
fc0e40014304030700000b0201AABBxxxxcc
                        ^^ ^^ ^^^^
                        |  |  account hash (yours will differ)
                        |  account selector
                        version
```

`AABB` is that phone's account hash and `xxxx` the resulting CRC — both differ per
account, so the bytes above are a template, not something to replay.

## Reading the state back

The account hash appears in **no GET response**. Over `SPPSERVICE4` the phone only
ever GETs attributes `0x0001` and `0x0002`, and both are device-info dumps —
model, serial, battery — with no account field.

The stored state is only visible in a NOTIFY (`msgID 0x45`) whose payload starts
`02 05 4c 0b`. The buds push it when a peer record is re-evaluated, and the only
trigger we could reproduce is an `MDE_VERSION` write. Opening the channel is not
enough on its own: three listen-only runs of 45–90 s, with the audio route
changing and a second host connecting during them, produced plenty of other
`0x45` traffic and no state frame at all. That is a real limitation, not a
formality — see [experiments.md](experiments.md#does-the-write-persist).

```
02 05 4c 0b 00 80 02 01 AA BB 01 01 ...  eb 1a 00 CC DD ...
            ^^^^^ ^^    ^^^^^          anchor    ^^^^^
            |     |     |                        peer's account (LE)
            |     |     this host's declared account (LE), offset 8-9
            |     asVer stored for this host, offset 6
            variant marker
```

Two independent anchors give the same value, so a parser can cross-check itself:

- offset 6 — `asVer` for the host that is talking
- offset 8–9 — the account hash that host declared, little-endian
- the two bytes after the literal `eb 1a 00` — the *other* peer's account hash

`budsmp read` uses this. Because a NOTIFY is only pushed on re-evaluation, it
first sends a version-only `MDE_VERSION` carrying the value the device already
holds. That write is a no-op for the record — critically, the short blob form does
not touch the account field at all — but it makes the buds emit the state frame.

### A write reports twice, and the first report is the old value

A write does not produce one state frame; it produces a short burst of them, and
they are a **before/after trace**. The earliest frames describe the record as it
was, the last one describes it as it now is. On one run an `apply` that set
`asVer = 2` reported, in order, `1 → 1 → 2 → 2`.

That is worth knowing for two reasons. It is the only way a host can observe a
value it did not itself just write — read the first frame, not the last, and you
have the previous contents of the record. And it is a trap for a naive parser:
taking the most common value across the burst reports `1` for a write that
plainly succeeded, because a 2–2 split has no majority. `budsmp` reports the
**last** decoded frame, and prints the whole sequence whenever the frames
disagree.

## Persistence

`asVer` and the account fields live in per-peer records inside the buds, and the
write is scoped to one host address — pair a second computer and that one needs its
own write.

**It is not permanent.** Measured on Galaxy Buds2 Pro with a macOS host:

| Event | `asVer` afterwards |
| --- | --- |
| right after `apply` | `2` — multipoint works |
| disconnect, reconnect a minute later | `2` — still set |
| both buds in the case, then taken out | `1` — back to blocked |

A stored `1` is how a cleared field looks: a written `0` reads back as `1` too, so
`1` means "nothing was ever set here". The symptom is unmistakable from the host
side — the buds accept the RFCOMM connection and then drop it
(`channel closed by peer`) the moment the phone connects, which is the stage-1 gate
doing its job.

The firmware explains the split exactly. The peer records are an array in on-die
RAM, not in flash; boot `memset`s each one and writes no default into `+0x4f`, and
the routine that repopulates a record when a peer connects restores the account
fields from non-volatile storage but reads `asVer` from the live record and writes
the same byte straight back. So the value is kept alive only by the RAM holding it:
an ordinary disconnect leaves it untouched, a power cycle takes it with everything
else, and there is nothing a host can send to make it stick.
[firmware-gate.md](firmware-gate.md#where-the-record-lives-and-why-the-fix-evaporates)
has the addresses.

So `apply` is a per-power-session command, not a one-time patch: run it whenever the
buds have powered on. Two other things clear the byte through the same reset —
giving a device a record for the first time, and fully releasing one — so removing
and re-adding your computer costs a write as well. See
[experiments.md](experiments.md#does-the-write-persist) for the measurement, and
[asver-lifetime.md](asver-lifetime.md) for the full account: every instruction that
can write the byte, every path that clears it, and why no sequence of frames makes
it durable.

On macOS you can hand the re-applying to a background agent instead of remembering
it — see [macos/README.md](../macos/README.md#keeping-it-applied).
