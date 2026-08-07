# Experiments on live hardware

Two questions could not be answered by reading the firmware, so they were settled
on a real device: whether the fix needs a Samsung account hash, and whether the fix
stays applied. The first answer was the good one.

## Is the account hash required? (No.)

The first working version of this fix replayed a phone's own account hash
alongside `asVer = 2`, which satisfied both firmware gates at once and left open
which one had actually been the problem. That mattered: if the account hash were
required, every user would need to capture HCI traffic from a paired Galaxy phone
first, and anyone without a Galaxy phone could not use this at all.

Static analysis could not settle it. The account gate skips the comparison when a
peer is flagged "special", but the code that sets that flag sits in a region where
function boundaries were not recovered from the firmware image. So it was settled
on hardware.

### Method

With a phone and a Mac both connected and multipoint working, overwrite the Mac's
record so that only the account field is wrong, keep `asVer = 2`, then force the
gate to re-evaluate by causing an `AUDIO.UP` event (start playback on one device
while the other is connected) and watch whether the phone survives.

Because `asVer` stays valid, stage 1 cannot fire — so a disconnect can only come
from stage 2, the account check. Each case is undone by writing the record again;
nothing here requires re-pairing.

Below, `AABB` stands for the account hash of the phone under test. Substitute your
own — `budsmp read` reports it, and `budsmp frame --asver 2 --account AABB` builds
the matching frame.

| case | frame | `asVer` | account declared by host |
|---|---|---|---|
| baseline | `budsmp frame --asver 2` | 2 | untouched |
| wrong hash | `budsmp frame --asver 2 --account dead` | 2 | `0xdead` (wrong) |
| zeroed account | `budsmp frame --asver 2 --account 0000 --selector 0` | 2 | absent |
| positive control | `budsmp frame --asver 0` | 0 | untouched (still correct) |

The positive control is the important one. Without it, "the phone stayed
connected" is unfalsifiable — it could just mean the gate never ran.

### Results

Telemetry read back from the `02 05 4c 0b` state NOTIFY after each injection,
alongside what the phone did:

| injected | `asVer` reported | account reported | phone |
|---|---|---|---|
| baseline, version-only | 2 | `0x0000` | connected |
| **wrong hash `dead`** | 2 | `0xdead` | **stayed connected, audio fine** |
| **`asVer = 0`**, account correct | **1** (firmware normalised 0 up to 1) | `0xAABB` | **dropped** |
| **account zeroed `0000`** | 2 | `0x0000` | **stayed connected** |
| restore `asVer = 2` | 2 | — | connected, multipoint working |

### Conclusion

The gate is not dormant: with the correct account and `asVer = 0` the phone was
dropped, exactly as `TwfConnAudioConnection_Changed` predicts for reason `0xa9`.
And yet with `asVer = 2` the phone survived an account that was wrong (`dead`) and
an account that was absent (`0000`) — including through an audio handover, which
forces a fresh evaluation.

So a computer is classified "special" by this firmware, the account comparison is
skipped for it, and **`asVer ∈ {2, 3}` is the only real gate.**

This holds under either reading of the account path. Whether the injected account
reaches the comparison and is tolerated, or never reaches it because the peer is
special, the account hash is not required either way. Same tools and same frames as
the working fix, so the conclusion does not depend on which interpretation is right.

Hence the shipped fix: one version-only frame, `fc0b00014304030400000b021eafcc`.
No capture step, no account hash, no Samsung account.

### Reproducing this yourself

You do not need to — `budsmp apply` is the outcome — but if you want to verify the
gate on your own hardware:

```bash
# baseline: confirm multipoint works, and see what the buds have stored
./budsmp read

# positive control: break it on purpose, then trigger AUDIO.UP and watch the phone
./budsmp apply --asver 0
#   -> expect the phone to be dropped

# put it back
./budsmp apply
```

Keep the buds awake for each step; see the troubleshooting notes in the
[macOS README](../macos/README.md). Every step is a single record field write and is
undone by the next one — no re-pairing, no firmware modification, no risk to the
phone. The worst case is a temporarily disconnected phone, which is the very thing
being measured.

## Does the write persist?

Not across a power cycle of the buds.

It is an easy thing to get wrong, and this repo got it wrong first: `asVer` sits in
a per-device record next to your account hash, which reads like stored
configuration, and a write to it plainly kept working across reconnects. That is
enough to *feel* settled without anyone having gone back to look at the value later.
Then multipoint broke after the buds spent a while in their case, and the assumption
finally got measured.

### Why this is awkward to measure

The buds never volunteer their state. It is visible only in the `02 05 4c 0b`
NOTIFY, and the only trigger we could reproduce for that NOTIFY is an
`MDE_VERSION` write — which makes the obvious experiment circular. Any command
that provokes a report has already overwritten the value it was supposed to read.

The `watch` command exists to break that circle: connect, send nothing, listen. On
this firmware it does not work.

| run | listen window | nudge used | state frames |
|---|---|---|---|
| 1 | 45 s | wake tone stopped mid-listen | 0 |
| 2 | 60 s | playback started and stopped on the host | 0 |
| 3 | 90 s | tone stopped, then the phone connected | 0 |

The channel was healthy in all three: other `0x45` NOTIFY traffic arrived
throughout — battery levels, tag `0x1f` — so the buds were talking, they just never
re-evaluated the peer record. Run 3 ended in `channel closed by peer`, which is the
symptom under investigation rather than a bug in `watch`.

### The technique that did work

A write produces a *burst* of state frames, and that burst is a before/after trace
of the record: the first frames carry the old contents, the last the new. So the
first frame of an `apply` reveals what was stored before `apply` touched anything.
It is an odd way to read a value — you have to overwrite it to learn what it was —
but it is the only route this firmware offers.

### The measurement

One device, in order:

1. `apply` → `RESULT: OK asVer=2`, one state frame. Multipoint confirmed working
   with a phone and the Mac connected at the same time.
2. Mac disconnected, reconnected about a minute later, `apply` again → a single
   frame reporting `2`, i.e. the value was still there before this write. **A
   disconnect and reconnect does not clear it.**
3. Buds put in the case, lid closed, taken out again and reconnected. Multipoint was
   broken: with the Mac connected, connecting the phone dropped the Mac, and
   `budsmp` logged `channel closed by peer`.
4. `apply` → four state frames, `asVer as reported : 1 -> 1 -> 2 -> 2`. **The
   stored value had been `1`.** The write fixed it on the spot; multipoint worked
   again, with no re-pairing.

Step 4 is the result. A `1` is precisely how a *cleared* field reads back — the same
off-by-one as in the account experiment above, where a deliberately written `0` also
came back as `1`. And `1` is outside `{2, 3}`, so stage 1 of the gate tears the link
down, which is exactly the `channel closed by peer` of step 3.

### Why it behaves that way

The measurement above left one thing open — the buds had been power-cycled *and*
several minutes had passed, so the trial alone could not say which mattered. The
firmware settles it: the peer records live in on-die RAM rather than flash, boot
clears them and writes no default into the `asVer` byte, and the routine that
repopulates a record on connect restores the account fields from non-volatile
storage while writing `asVer` back over itself. Nothing persists the byte, and
nothing would load it if it were persisted.

That predicts precisely the split that was measured — survives a reconnect, dies
with the power — and it also means there is no host-side trick worth looking for.
The addresses are in
[firmware-gate.md](firmware-gate.md#where-the-record-lives-and-why-the-fix-evaporates).

### Checking it on your own buds

Two writes and a trip to the case:

```bash
./budsmp apply          # note "asVer=2"
#   ... put the buds in the case, take them out, reconnect ...
./budsmp apply          # read the FIRST value in "asVer as reported"
```

If that first value is `1`, the record was cleared while they were away. `budsmp`
prints the full sequence whenever the frames disagree, so you do not have to parse
anything by hand:

```
  asVer (this host)  : 2   [multipoint allowed]
  asVer as reported  : 1 -> 1 -> 2 -> 2
                       (first value is what was stored before the write)
```
