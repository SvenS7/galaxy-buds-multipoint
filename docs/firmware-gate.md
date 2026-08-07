# Why the buds hang up on your phone

## Symptom

You connect a Mac (or a PC, or anything that is not a Galaxy device) to a pair of
Galaxy Buds2 Pro. The moment audio comes up on the second device, the buds drop
the first one. Multipoint is advertised and works fine between two Galaxy
devices, but a non-Galaxy host always ends up being an exclusive connection.

The common explanation is that the buds refuse "heterogeneous" links, or that
they reject a device whose Bluetooth Class of Device says "computer". Neither is
what happens.

## The actual gate

When audio comes up on a second device the firmware runs a check in
`TwfConnAudioConnection_Changed`. It has two stages, in this order:

1. **`asVer` gate.** Each peer record holds an `asVer` byte at `record[0x4f]`. If
   *either* peer's `asVer` is outside `{2, 3}`, the firmware tears the link down
   with disconnect reason **`0xa9`**.

2. **Account gate.** Only reached if stage 1 passed. `IsAccountValidByDeviceId`
   compares the two peers' stored Samsung-account hashes; a mismatch disconnects
   with reason **`0xab`**. There is an exception: if one peer is classified
   "special" — `record[0x44] == 4` or `record[0x3e] == 1` — the account of the
   normal peer alone is enough and no match is required.

Only one thing in the firmware can set `asVer` to a value that passes the gate:
the `MDE_VERSION` handler. Six instructions in the image write the field at all,
and the other five either write the constant `1`, copy the byte onto itself, or
belong to bud-to-bud sync — the full table is in
[asver-lifetime.md](asver-lifetime.md#3-what-is-allowed-to-write-it--confirmed-for-this-image).

A Galaxy phone sends `MDE_VERSION`, so it gets `asVer = 2`. Third-party clients
connect to `GEARMANAGER` (ch 27) and never touch `SPPSERVICE4` (ch 29), so they
never send it, and their `asVer` stays **0**.

When the phone sends it is less clear than it looks, and we could not settle it.
The frame turns up in a capture at re-pairing, and the code that builds it is in
none of the Android trees we could read — `SPPSERVICE4` belongs to the platform
Bluetooth stack, and the connection path there sends other things but not this.
Pairing-time registration is the natural reading, but it cannot be the whole
story: the record is wiped every time the buds boot (see below), and phone-to-buds
multipoint keeps working after a spell in the case. So either something on the
phone re-registers per power-on, or a phone clears stage 1 by a route we have not
found. [asver-lifetime.md](asver-lifetime.md#5-when-does-a-phone-send-mde_version--honest-unknown)
lays out what is and is not known. Either way it makes no difference to a host:
`budsmp apply` is a per-power-session command, because the byte it writes does
not outlive a power cycle.

That is the whole bug. A non-Galaxy host fails at stage 1 with `0xa9` and never
reaches the account check at all. The behaviour looks account-related because
account-less hosts are exactly the hosts that also never send `MDE_VERSION` — but
the two are independent, and it is the second one that matters.

## What about the account?

Once `asVer` is fixed, stage 2 turns out not to block a computer either: the
firmware classifies such a host as "special", so the account comparison is
skipped. This was not obvious from static analysis — the code that sets the
special flag sits in a region where function boundaries were not recovered — so it
was settled on real hardware instead. Three cases were injected while a phone was
connected: a deliberately wrong account hash, a zeroed account, and (as a positive
control) `asVer = 0` with the correct account.

Wrong account: phone stays. No account at all: phone stays. Correct account but
`asVer = 0`: phone drops, reason `0xa9`. The gate is alive and it only cares about
`asVer`. Full method and logs in [experiments.md](experiments.md).

So the minimum viable fix is one frame that writes `asVer = 2` and touches nothing
else. You do not need a Samsung account, you do not need to capture your phone's
account hash, and you do not need to replay it.

## What about "hetero ACL"?

`TwfLe_IdentifyHeteroBredrLink` is often assumed to be the blocker. Reading it,
the comparison it performs is a 6-byte memcmp of Bluetooth addresses — it asks
"is this a second device at a different address", which is just how multipoint
bookkeeping identifies peers. It does not inspect the Class of Device and it does
not reject anything. It is not the lever.

## Where the record lives, and why the fix evaporates

The fix does not survive the buds powering down — measured on hardware, and the
firmware says exactly why.

The peer records are not in flash. They are an array in on-die RAM at
`0x2057AE38`, `0x127` bytes per peer, five slots; `GetInternal@0x17672c` returns
`base + 0x127*id + 0xB` and `asVer` is `+0x4f` inside that.

At boot, `TwuConn_Init@0x179824` loops over ids 0–4 and calls the record
initialiser at `0x175e8c` with `arg = 3`, the full-reset variant:
`memset(record, 0, 0x127)`. It then restores a handful of fields by hand —
`+0xd`, `+0x19`, `+0x21`, `+0x2d`, `+0x2e`, plus two flag bits in `+0x6f` — and
`+0x4f` is not one of them. So every power-on leaves `asVer = 0` on every slot,
and nothing loads it back from flash.

The connection-time restore path looks like it ought to help. It does not.
`TwfConnDeviceSetting_UpdateConnectedRecord@0x228180` genuinely does repopulate a
record when a peer connects, but for `asVer` it reads the *live* byte and writes
the same byte back:

```
228386  ldrb.w r3, [r3, #0x4f]    ; from the live runtime record
22838A  strb.w r3, [r7, #0x14a]   ; the only writer of this local, function-wide
  ...
2285B2  ldrb.w r2, [r7, #0x14a]
2285BA  strb.w r2, [r3, #0x4f]    ; same value, same record — a no-op
```

The bytes that *do* come out of non-volatile storage land in neighbouring locals
(`+0x14b`, `+0x1ed`) and feed the account fields. That asymmetry is the whole
story: **your account hash is restored on connect, `asVer` never is.**

The `MDE_VERSION` handler does call `TwfConnDeviceSetting_SetRecord@0x228F98`,
which packs `asVer` into a RAM shadow copy and sets a dirty flag — but the actual
norflash flush (`TwaSetting_Flush@0x263ee0`) is only reached from factory, AT
command and boot-load contexts, never from a setting write. And per the paragraph
above, flushing it would not matter anyway, because nothing reads it back.

So **a host cannot make this durable.** Pinning `asVer` across a power cycle needs
a firmware change: a default of `2` in the record initialiser, a restore path that
actually loads the stored byte, or a handler that refuses to downgrade. From the
host side the only option is to write it again, which is what `budsmp apply` is.

Three paths clear the byte, and all three are the same full `memset`:

| path | when it runs |
|---|---|
| `TwuConn_Init@0x179824` | boot — i.e. every time the buds power on |
| `TwuConnDevice_UpdateConnection` `@0x1768c6` | a device is given a slot for the first time |
| same function `@0x176d1a` | a slot is fully released (`record[0x24] == 0 && record[0x2c] == 0`) |

The partial re-inits used on an ordinary disconnect (`arg = 1` at `0x176bd6`,
`arg = 2` at `0x176c80`) leave `+0x4f` alone. That is precisely why the fix
survives disconnecting and reconnecting but not a spell in the case. The link-key
"key missing" clear at `0x229B34` writes a different table altogether and does not
touch `asVer` either.

Addresses are file offsets into `seg6.bin` of firmware `R510XXU0AZD1`; other
builds will differ. The three claims this section rests on — the boot `memset`,
`+0x4f` getting no default, and the restore being a no-op — were each read off the
disassembly directly. The hardware measurement is in
[experiments.md](experiments.md#does-the-write-persist), and the full walk through
the byte's lifetime — every writer, every clear path, and why no host can pin it —
is in [asver-lifetime.md](asver-lifetime.md).

## Field reference

| field | offset | meaning |
|---|---|---|
| `asVer` | `record[0x4f]` | accessory protocol version; must be 2 or 3 |
| account selector | `record[0x51]` | which account identity slot is in use |
| account hash | `record[0x52]` | 2-byte Samsung account hash |
| special marker | `record[0x44]`, `record[0x3e]` | `== 4` / `== 1` skips the account match |

| reason | meaning |
|---|---|
| `0xa9` | `asVer` outside `{2, 3}` |
| `0xab` | account mismatch between two normal peers |

Writing `asVer = 0` normalises to `1` in the stored state — either way it is
outside the pass set, which is what makes `budsmp revert` a clean undo. The
normalisation happens in the `MDE_VERSION` handler before the store: a received
`0` is replaced with `1` at `0x22AC40`–`0x22AC64`, and `0x22ACB6` writes that
local into the record. `> 3` is the only value the handler rejects outright.

A freshly booted record also reads back as `1` rather than the `0` the `memset`
leaves behind, for a separate reason: a connection-time path at `0x22F308` writes
`1` into the field whenever it is not already `2` or `3`. Neither behaviour
matters to the gate — `0` and `1` both fail it — but both matter when you read a
value back, because `1` is what "nothing was ever set here" looks like from a
host. Both are traced in
[asver-lifetime.md](asver-lifetime.md#nothing-above-3-gets-in-and-0-becomes-1).
