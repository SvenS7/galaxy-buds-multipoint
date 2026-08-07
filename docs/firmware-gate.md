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

`asVer` is written by exactly one thing: the `MDE_VERSION` handler. Nothing else
in the firmware sets that field.

A Galaxy phone sends `MDE_VERSION` during pairing, so it gets `asVer = 2`.
Third-party clients connect to `GEARMANAGER` (ch 27) and never touch
`SPPSERVICE4` (ch 29), so they never send it, so their `asVer` stays **0**.

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
outside the pass set, which is what makes `budsmp revert` a clean undo.
