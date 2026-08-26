# de-MLS Reliability Simulation

A local simulation of the **reliability** question in de-MLS: when a steward
commits over an unreliable transport (Waku / logos-delivery), some members miss
the commit. Does the group converge on **one** commit (success) or diverge
(fork) / get stuck (stall)?

No MLS or de-MLS crypto is simulated. Only message delivery, loss, and the
sync logic on top of it.

## Model

- **Event-driven, no rounds.** Every message carries a timestamp and is
  delivered after a random delay, or dropped. This lets commits and reactions
  race naturally.
- **One shared channel** (mirrors a Waku pubsub topic): a broadcast reaches
  every other participant, and each delivery is dropped independently with
  probability `p_loss`. A broadcast that reaches `k` members costs `k`
  messages, drops included.
- **Roles:** one Epoch Steward (ES) produces the canonical commit; a Backup
  Steward (BS) may produce its own if it misses the ES commit; the rest are
  members.
- **Single epoch per trial.** Rotation / multi-epoch is out of scope here.
- **Per-participant log** so any member's history can be inspected.

### Success criterion

Global: **all** participants end on the exact same commit. Anyone on a
different commit (**fork**) or on no commit (**stall**) fails the trial. Stall
is a softer failure than fork and is reported separately.

### Diagnosis (type 2 vs type 3)

A member that lacks the ES commit cannot tell *why*:

- **got-es**: received the ES commit directly.
- **type 2 (afk)**: never saw the commit and never reached quorum, so it
  concludes the ES was absent.
- **type 3 (network failed)**: saw a quorum of confirmations (the commit
  provably exists) but the body never arrived; the ES is fine, the network
  dropped it.

## Scenarios

The simulator implements four, selectable by flag. Later ones build on earlier.

### `--plain` (no sync)

The steward commits; members apply whatever commit they hold, preferring the
ES's over the BS's. No recovery. A member that misses the ES commit but holds
the BS commit applies a *different* commit than the rest. **This forks by
design** and is the baseline.

### `--reflexion` (basic sync)

Members that miss the ES commit broadcast **NO-ES**. Any holder re-broadcasts
the full commit in response. A member that sees the same commit from
`2n/3` distinct senders is synced. Also classifies type 2 vs type 3.

This removes forks (turning them into stalls) but is expensive: the full,
heavy commit is re-broadcast by every holder, so traffic is `O(n^2)`.

### `--reflexion2` (light attestation + body pull)

Splits the heavy commit from the light confirmation:

- **ATTEST** (light, hash only): "I have seen commit X." Quorum is counted over
  distinct ATTEST senders, not over re-sent bodies.
- **Suppression:** a member waits a short random time before attesting; if it
  already sees quorum, it stays silent, so far fewer than `2n/3` actually send.
- **Body pull:** a member that has quorum attestations but lacks the body sends
  a **BODYREQ**; any holder answers with the heavy body by **unicast** (only to
  the requester, not the whole channel).
- **Grace period:** a member does not request the body the instant it sees an
  attestation; it waits briefly in case the body is still in flight, then
  requests only if still missing. This removes a burst of needless requests.
- **Retry:** if the body is dropped, the request is retried a bounded number of
  times.

Same `2n/3` quorum, same security, but the heavy body no longer floods the
channel. Sync requires **both** quorum attestations and the body, which is
slightly stricter than reflexion, so in tiny groups it is a little less robust.

### `--committee` (control committee) — the optimized design

Only a deterministic **control committee** attests; everyone else just consumes
the result.

- **Committee selection:** deterministic, `SHA256(epoch || member_id)` sorted,
  smallest `committee_ratio` fraction (default 20%). Everyone recomputes it, so
  no one can lie about being on it, and it rotates every epoch.
- **Decision:** committee members attest on the shared channel; the threshold is
  the **committee-internal 2/3** (e.g. 134 of a 200-member committee), not the
  global `2n/3` (667). This is where the traffic drops.
- **Distribution:** the committee only agrees on the commit **hash**; the heavy
  body is fetched from **any** holder and verified against that hash, so the
  committee never carries body traffic and is never blindly trusted.

Security is preserved without BLS: the committee is deterministic and needs its
own 2/3 honest majority; because it is small, carrying its signatures plainly
(no aggregation) is affordable.

## Results

### Big group, n = 1000 (the case that matters)

Total messages per trial (drops included), committee vs reflexion2:

| p_loss | reflexion2 msgs | committee msgs | committee / reflexion2 | success |
| -----: | --------------: | -------------: | ---------------------: | ------: |
|  0.001 |         ~1.00 M |         ~209 K |                  ~0.21 |    100% |
|  0.01  |         ~1.01 M |         ~220 K |                  ~0.22 |    100% |

Committee cuts total traffic ~**4.6–5x** with identical success. The factor
matches the design: attestation drops from every member (~1000) to just the
committee (~200).

Message breakdown at n=1000, p=0.01, committee:

| kind          | count  | note                                  |
| ------------- | -----: | ------------------------------------- |
| ATTEST        | ~198 K | committee decision, the dominant cost |
| BODYREQ       |   ~7 K | body pull, small thanks to the grace  |
| COMMIT (uni)  |   ~7 K | body answers, unicast to requester    |
| COMMIT (bcast)|   ~1 K | ES's initial broadcast                |

Two observations:

1. After the committee split, the decision cost is small; what remains is the
   committee simply speaking (~200 senders x ~1000 receivers). That is a floor
   set by Waku's broadcast nature, not by packet loss.
2. The **grace period** removed a large burst of premature body requests. Going
   from p=0.1% to p=1% raised the total only ~5% (209K -> 220K), so loss adds
   very little overhead now. Earlier, without the grace, a member seeing an
   attestation slightly before the body would needlessly request it, producing
   ~360K wasted messages.

### Big group robustness

Even at high loss, reflexion2 / committee do **not** fork in a large group;
failures (if any) are stalls, never forks. With n=1000 a member almost always
receives the commit from at least one of the hundreds of holders, so
redundancy rises with group size. A 1/3 offline share is not fatal at n=1000;
it is at n=12.

### Small group, n = 12 (300 trials), success rate

| p_loss | plain | reflexion | reflexion2 | committee |
| -----: | ----: | --------: | ---------: | --------: |
|  0.1   |  0.34 |      1.00 |       0.99 |      0.94 |
|  0.3   |  0.04 |      1.00 |       0.74 |      0.53 |
|  0.5   |  0.00 |      0.88 |       0.50 |      0.11 |
|  0.7   |  0.00 |      0.16 |       0.14 |      0.00 |

Committee is **not** meant for tiny groups: 20% of 12 is a 2–3 member
committee, too small for its own 2/3 to be meaningful. Committee is a
large-group optimization; in small groups plain reflexion is more robust.

## Takeaways

- **Plain forks by design.** A sync mechanism is required, confirming the
  reported issue.
- **The expensive part is the decision, not the distribution.** Re-sending the
  heavy body (reflexion) is what makes it `O(n^2)`; splitting attestation from
  body (reflexion2) and restricting attestation to a committee (committee) both
  attack that. Body distribution was always `O(n)` and is not the bottleneck.
- **A control committee cuts decision traffic ~5x** at n=1000 with no loss of
  success and without BLS, using only a deterministic committee + its own 2/3
  threshold + hash-verified body pull.
- **The remaining floor is Waku's broadcast cost**: the committee speaking to
  the whole topic. Lowering it further means a smaller committee (more variance)
  or signature aggregation (avoided here on purpose).
- **Group size helps.** Reliability improves as the group grows, because
  redundancy grows; the hard cases are small groups at high loss.

## Files

- `demls_sim.py` — the simulator (all four scenarios, config-driven).
- `test_demls.py` — 11 self-checking requirements; all pass.
- `report.md` — this file.

## Running

```bash
# self-check
python3 test_demls.py

# single run
python3 demls_sim.py --plain      --p-loss 0.2 --stewards 3 --members 9  --trials 300
python3 demls_sim.py --reflexion2 --p-loss 0.3 --stewards 3 --members 9  --trials 300
python3 demls_sim.py --committee  --p-loss 0.01 --stewards 3 --members 997 --trials 5
```

Config lives in the `Config` dataclass in `demls_sim.py`, each field commented.
Key ones: `p_loss`, `malicious_ratio`, `committee_ratio`, `bodyreq_grace`,
`suppress_wait`, and the `2n/3` quorum via `quorum_num`/`quorum_den`.
