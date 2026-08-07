# Is the account hash required? (No.)

The first working version of this fix replayed a phone's own account hash
alongside `asVer = 2`, which satisfied both firmware gates at once and left open
which one had actually been the problem. That mattered: if the account hash were
required, every user would need to capture HCI traffic from a paired Galaxy phone
first, and anyone without a Galaxy phone could not use this at all.

Static analysis could not settle it. The account gate skips the comparison when a
peer is flagged "special", but the code that sets that flag sits in a region where
function boundaries were not recovered from the firmware image. So it was settled
on hardware.

## Method

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

## Results

Telemetry read back from the `02 05 4c 0b` state NOTIFY after each injection,
alongside what the phone did:

| injected | `asVer` reported | account reported | phone |
|---|---|---|---|
| baseline, version-only | 2 | `0x0000` | connected |
| **wrong hash `dead`** | 2 | `0xdead` | **stayed connected, audio fine** |
| **`asVer = 0`**, account correct | **1** (firmware normalised 0 up to 1) | `0xAABB` | **dropped** |
| **account zeroed `0000`** | 2 | `0x0000` | **stayed connected** |
| restore `asVer = 2` | 2 | — | connected, multipoint working |

## Conclusion

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

## Reproducing this yourself

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
[macOS README](../macos/README.md). Every step is a single NVRAM field write and is
undone by the next one — no re-pairing, no firmware modification, no risk to the
phone. The worst case is a temporarily disconnected phone, which is the very thing
being measured.
