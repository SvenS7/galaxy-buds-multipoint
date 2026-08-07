# The life of one byte

Everything inconvenient about this fix comes from a single byte — `asVer`, at
`record[0x4f]` of a peer record inside the buds. Write `2` there and multipoint
works; the byte goes away and you are back where you started.

This is the long answer to four questions about it:

1. Is it persistent?
2. What erases it?
3. What is allowed to write it?
4. Can a host make it stick across a power cycle?

Short version: it lives in RAM, boot wipes it, only six instructions in the
firmware can write it, and no — a host cannot pin it. The rest of this page is
the evidence.

Addresses are file offsets into `seg6.bin` of firmware `R510XXU0AZD1` (Galaxy
Buds2 Pro); other builds will differ. Every address below was read off the
disassembly for this document. Claims that are *not* read directly say so, in
the same breath.

---

## 1. It lives in RAM, and boot wipes it — confirmed

The peer records are an array in on-die memory at `0x2057AE38`: `0x127` bytes
per peer, five slots. `GetInternal@0x17672c` hands out `base + 0x127*id + 0xB`,
and `asVer` is `+0x4f` inside that. A `0x20xxxxxx` address on this part is
SRAM — nothing about it survives losing power.

At boot, `TwuConn_Init@0x179824` walks ids 0–4 and calls the record initialiser
at `0x175e8c` with `arg = 3` (the call is at `0x179864`). That branch of the
initialiser is:

```
175EA6  movw   r2, #0x127
175EAA  movs   r1, #0
175EAC  ldr    r0, [r7, #4]
175EAE  bl     #0x2705e8            ; memset(record, 0, 0x127)
```

and then it hand-restores exactly five fields — `+0x19`, `+0x21`, `+0xd`,
`+0x2d` and `+0x2e` (the last two to `0xff`) — and ORs two flag bits into
`+0x6f`. `+0x4f` is not among them, and nothing in the function reads flash.

So **every power-on starts every slot at `asVer = 0`**, and there is no default
and no restore. Which is exactly what the hardware measurement shows, in
[experiments.md](experiments.md#does-the-write-persist).

## 2. What erases it — confirmed

There is exactly one mechanism that zeroes the byte: the full `memset` above.
The initialiser has five call sites in the whole image, and the argument at each
one is a literal, so the table is complete:

| call site | arg | when it runs | clears `asVer`? |
|---|---|---|---|
| `0x179864` — `TwuConn_Init@0x179824` | 3 | boot, i.e. every power-on | **yes** |
| `0x1768C6` — `TwuConnDevice_UpdateConnection` | 3 | a device is given a slot for the first time | **yes** |
| `0x176D1A` — same function | 3 | a slot is fully released | **yes** |
| `0x176BD6` — same function | 1 | ordinary disconnect (partial re-init) | no |
| `0x176C80` — same function | 2 | ordinary disconnect (partial re-init) | no |

The `arg = 1` and `arg = 2` branches touch `+0x19`–`+0x1d`, `+0x77` and
neighbours; a sweep of the entire initialiser finds no reference to offset
`0x4f` outside the `memset`. That is the whole explanation for the measured
split: **a disconnect keeps the fix, a trip to the case loses it.**

Two things follow that are easy to miss. Removing your computer from the buds
and re-adding it costs a write too, because a fresh slot goes through the same
`arg = 3` path. And a "link key missing" event does *not* cost one:
`TwfConnDeviceSettingRecord_SetKeyMissing` operates on the 16-byte-stride
device-setting table, and no peer-record `+0x4f` write exists anywhere in that
family of functions.

## 3. What is allowed to write it — confirmed for this image

Method: a linear Thumb-2 sweep of `seg6.bin` for every instruction carrying a
literal `#0x4f` offset, restarting past the bytes the disassembler rejects so
literal pools do not end the scan early; then keep only the stores; then keep
only the ones whose destination pointer came from `GetInternal@0x17672c`. Six
survive.

| address | writes | what it is |
|---|---|---|
| `0x22ACB6` | the received version byte | `MDE_VERSION`, version-only form — **the frame `budsmp` sends** |
| `0x22AEF8` | the received version byte | `MDE_VERSION`, account-carrying form |
| `0x22A936` | a local | error/overwrite path; the function returns `0xFFFFF731` |
| `0x206EE0` | `peer_info[0x5f]` | bud-to-bud device-info sync |
| `0x2285BA` | the record's own live byte | connect-time restore — a no-op, see §4 |
| `0x22F308` | always the literal `1` | see below |

Everything else in the image that mentions `#0x4f` is either a stack local
(`[r7, #0x4f]`), the FOTA descriptor (`0x2351AE`, `0x235278`, `0x2353DA`,
`0x235E3C` — same offset, different struct), or one of two writes to structures
that are not peer records: the global at `0x2051266C` (`0x206250`) and a
message-carried struct (`0x2081A8`). Neither of those two goes through
`GetInternal`.

One caveat on "complete": the sweep finds literal offsets. A write that reached
the field through a computed offset, or as part of a bulk copy, would not appear
in it.

### Nothing above 3 gets in, and 0 becomes 1

The `MDE_VERSION` handler validates loosely and normalises:

```
22AC1E  ldrb.w r3, [r7, #0x2a]      ; the received version byte
22AC22  cmp    r3, #3
22AC24  bhi    …                    ; > 3 is the only rejection
  ...
22AC40  ldrb.w r3, [r7, #0x2a]
22AC44  cmp    r3, #0
22AC46  beq    #0x22AC56
  ...
22AC56  movw   r3, #0x917           ; log line number
22AC62  movs   r3, #1
22AC64  strb.w r3, [r7, #0x2a]      ; 0 becomes 1
```

and `0x22ACB6` stores that same local into `record[0x4f]`. So a host can write
`0`–`3`, and a written `0` is stored as `1`. `1` still fails the gate, which is
what makes [`budsmp revert`](../README.md) a clean undo — and it is also why a
byte you did not set reads back as `1`.

### `0x22F308`: the path that only ever writes 1

The function at `0x22F1B0` — one arm of the handler table dispatched at
`0x231DA0` — reads `record[0x4f]` and `record[0x50]` into locals, checks a
predicate (`bl 0x25c118`), and if that predicate does not let it out early:

```
22F2AE  movs   r3, #1
22F2B0  strb   r3, [r7, #0x1e]      ; the local is now, and stays, 1
  ...
22F2C8  strb.w r2, [r3, #0x50]      ; record[0x50] = 1
  ...
22F2E0  ldrb.w r3, [r3, #0x4f]
22F2E8  cmp    r3, #2
22F2EA  beq    #0x22F30C            ; already 2 — skip
22F2EE  cmp    r3, #3
22F2F0  beq    #0x22F30C            ; already 3 — skip
  ...
22F308  strb.w r2, [r3, #0x4f]      ; otherwise record[0x4f] = 1
22F30E  bl     #0x228F98            ; SaveSettingNV
```

It cannot undo the fix — `2` and `3` are skipped — and it cannot create it,
because `1` is the only value it ever writes. It is the second reason a cleared
record reads back as `1` rather than the `0` the `memset` left.

## 4. No host can make it durable — confirmed on every path that matters

The `MDE_VERSION` handler's one gesture toward persistence is
`SaveSettingNV` — `TwfConnDeviceSetting_SetRecord@0x228F98`. It builds a 16-byte
settings entry, hands it to the record writer at `0x22949C`, and marks the
in-RAM settings image dirty. The actual flush to norflash,
`TwaSetting_Flush@0x263ee0`, has six call sites in the image, and the log
strings in each one name them:

| call site | enclosing function |
|---|---|
| `0x1E4C10` | `TwfFactoryRework_OnTimeout` |
| `0x1EF100` | `TwfFactoryBattery_Handle` |
| `0x1F2F96`, `0x1F3030` | `TwfFactoryFsmStIdle_OnTWU_MSG_ID_FACTORY_AT_CMD_DATA` ("Start Factory Reset…") |
| `0x20722C` | `TwfSettingLoaded_InternalComplete` |
| `0x20CC14` | `TwfPlc_MultiBytedata` |

Factory paths, an AT-command path, the boot settings load, and a PLC handler.
Not one of them is on the setting-write path, the disconnect path, or the
case-insertion path.

**And a flush would not help anyway**, which is the part that closes the
question. The connect-time restore,
`TwfConnDeviceSetting_UpdateConnectedRecord@0x228180`, genuinely does repopulate
a record from non-volatile storage — but not this field. For `asVer` it reads
the *live* byte and writes the same byte back:

```
228386  ldrb.w r3, [r3, #0x4f]      ; from the live runtime record
22838A  strb.w r3, [r7, #0x14a]     ; the only writer of this local, function-wide
  ...
2285B2  ldrb.w r2, [r7, #0x14a]
2285BA  strb.w r2, [r3, #0x4f]      ; same value, same record
```

The bytes that *do* come out of storage land in neighbouring locals and feed the
account fields. So your account hash is restored on connect and `asVer` never
is — write-only from the gate's point of view.

There is one byte in the image that looks like a persisted `asVer` and is not.
NV offset `+0x1a8` is copied into `+0x4f` of the global device struct at
`0x2051266C` (`0x2081A8`, with `+0x1a9 → +0x4e` immediately after; `0x206250`
writes the same pair from a different source). It is a single global byte rather
than one per peer, and neither write goes through `GetInternal`. *Inferred:* we
did not trace the dispatcher chain above `0x2081A8` far enough to prove no
caller ever hands it a peer record — but nothing suggests one does, and if one
did, `asVer` would survive a case trip, which it measurably does not.

On the protocol side there is nothing to try either: `MDE_VERSION` has no
persist opcode, no persist flag and no long form. See
[protocol.md](protocol.md#mde_version-opcode-0x0b).

So durability needs a firmware change, and there are three obvious shapes it
could take — a default of `2` for `+0x4f` in the record initialiser, a restore
path that actually loads the stored byte, or a handler that refuses to downgrade
below `2`. All three mean unlocking and reflashing the buds, which is a very
different risk profile from sending one RFCOMM frame, and is not something this
project does or recommends.

## 5. When does a phone send `MDE_VERSION`? — honest unknown

This is the one question we could not close, and it is worth being explicit
about because it is the natural follow-up to §1: if boot wipes the record, how
does a phone keep working after the buds have been in the case?

What we can say:

- **The frame's constructor is not in any Android tree we could read.** The SMEP
  service on channel 29 belongs to the platform Bluetooth stack rather than to
  the Buds manager app, and the native JNI library carries no `MDE`- or
  `asVer`-related strings at all.
- **"The phone re-sends it on every connection" is refuted for the obvious
  path.** The component that owns channel 29 does run code on every
  connection — but what it sends there is a subscribe, an SBM capability query,
  an EIR read and an auto-switch-mode call. No version frame, no account frame.
  The tag id this fix uses does not even appear in that component's tag enum,
  and an unknown tag hits a default branch that logs a complaint and drops the
  message.
- **The positive evidence points at one-time registration.** In a capture taken
  while re-pairing a phone, the exchange appears as a short handshake at pairing
  time and nowhere else.

What we cannot say is how those two facts fit together. Either something outside
the code we read re-registers after a power cycle, or the phone passes the
stage-1 gate by a route we have not found. Both are *inferences*; we are not
picking one.

It makes no practical difference to the tool. Whatever the phone does, the host
has to write the byte once per power session, and that is what
[`budsmp apply`](../README.md) — or, on macOS,
[`budsmp daemon`](../macos/README.md#keeping-it-applied) — is for.

---

## Using this in practice

- One write per power session, per host address. A second computer needs its own.
- A disconnect and reconnect is free. The case is not.
- macOS can automate it: see [macos/README.md](../macos/README.md#keeping-it-applied).
- On Linux and Windows, re-run `apply`.

Related: [firmware-gate.md](firmware-gate.md) for the gate that reads the byte,
[protocol.md](protocol.md) for the frame that writes it,
[experiments.md](experiments.md) for what happened on real hardware.
