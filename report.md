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

## Follow-up: safety knobs, the RFC model, and efficiency

The scenarios above answer "does a sync mechanism help." A second round asks
three sharper questions: where is the *safety* boundary, how faithful is the
model to the RFC, and what does the RFC's commit collection *cost*. This added
four parameters, one metric, and six self-checks.

### Parameters added

| flag (CLI) | in one line | what it is for |
| --- | --- | --- |
| `--apply-only-on-quorum` | Don't settle on any commit until a 2n/3 quorum has confirmed it | safety switch: trade forks for stalls |
| `--equivocate` | Let a malicious member lie — attest a commit it does not hold | attack switch: probe the honest-majority limit |
| `--all-mint` | Every steward mints its own commit at once (the RFC model), instead of one primary steward committing and a backup only on failure | make the model faithful to the RFC |
| `--committee` | Only a deterministic committee attests (was already implemented, now reachable from the CLI) | large-group optimization |

The **honest-majority assumption** is not a new flag — it is the existing
`malicious_ratio` (`--malicious`). "At least 2n/3 honest" simply means
`malicious_ratio <= 1/3`; the safety results hold only below that line.

A metric was added too: `avg_convergence` — the mean sim time a group takes to
settle, over successful trials (the "timer" axis).

### Self-checks added (R12–R18)

| id | claim | evidence |
| --- | --- | --- |
| R12 | quorum-gated apply removes the residual fork | forks 89 → **0** (but 400 stalls: safe, not free) |
| R13 | a small committee is not safe by sampling | P(committee >= 1/3 Byz): n=12 **0.46** vs n=1000 **0.002** |
| R14 | < n/3 Byzantine cannot forge a commit | honest applied C-EVIL: mal=1/3 → **0**, mal=0.7 → **220** |
| R15 | RFC concurrent minting forks more than primary-first | forks **73 → 351** at the same loss |
| R16 | the sync layer resolves the RFC fork | plain **13/285** → reflexion2 **300/0** (success/fork) |
| R17 | all-mint costs messages, not time | msgs **157 → 241**, convergence **4.7s = 4.7s** |
| R18 | the committee-safety formula matches real sampling | analytic **0.087** vs sampled **0.083** (and 0.197 vs 0.193) |

### The model vs. the RFC

The original simulation is **primary-first**: one Epoch Steward commits, and a
Backup commits only if it never saw the primary's commit. The RFC (§Commit
validation service) is **concurrent**: "multiple stewards MAY issue commit
messages within the same epoch," and each member "MUST locally ...
deterministically [select] at most one valid commit" over **the commits it
happened to receive** — with no mechanism specified to make those received sets
equal.

`--all-mint` implements the RFC model: every steward mints a distinct commit at
once; members select the epoch steward's commit if held, else the smallest
committer id (the RFC rule when all stewards carry the same proposals). The
difference is stark and one-sided:

| p_loss | primary-first forks | all-mint forks |
| ---: | ---: | ---: |
| 0.05 | 13 | **156** |
| 0.10 | 36 | **262** |
| 0.20 | 73 | **351** |
| 0.30 | 113 | **384** |

The success rate is the same in both — they differ only in *how they fail*.
Primary-first fails **safe** (a member missing the commit has nothing to apply →
stall); all-mint fails **unsafe** (a member almost always holds *some* steward's
commit → applies a competitor → fork). So the original sim understated the real
problem 3–12x. The fork surface is set by the steward count: at `sn=1` all-mint
forks **0** (only stalls); `sn>=2` flips the failures almost entirely to forks.

### Collection efficiency (messages + timers)

Holding the sync layer constant and varying only the collection model:

| measure | primary-first | all-mint |
| --- | --- | --- |
| plain, msgs | 12 | **33** |
| reflexion2 p=0.1 — msgs / conv / success | 158 / 4.7s / 296 | **241** / 4.7s / **300** |
| reflexion2 p=0.2 — msgs / conv / success | 198 / 5.9s / 272 | **264** / 5.9s / **300** |

Commit traffic under all-mint scales linearly with the steward count
(`sn * (n-1)`), while primary-first stays flat:

| stewards | primary msgs | all-mint msgs |
| ---: | ---: | ---: |
| 1 | 11 | 11 |
| 3 | 12 | 33 |
| 6 | 12 | 66 |

Two facts fall out: all-mint costs **more messages but the same convergence
time** (latency is set by the attest+pull rounds, not by how many stewards
minted), and the extra broadcasts **buy robustness** — all-mint reflexion2
reaches 300/300 where primary-first gets 272, because the winner's body is
redundantly held and served.

### Main idea

Three plain findings, each backed by a test above.

1. **When every steward commits at once, one lost message forks the group.** In
   the RFC model all stewards commit together, so a member that misses the
   winning commit still holds someone else's and settles on that one — the group
   splits for good. When only one steward commits (or `sn=1`), a member that
   misses it has nothing to settle on and just waits, which is recoverable. The
   same lost message forks the group in one model but only causes a harmless
   wait in the other (R15: 73 → 351 forks). The number of stewards is the dial:
   `sn=1` never forks, `sn=2` already does. The cheapest way to cut forks is
   fewer stewards, not a longer timer.

2. **The fix: tell everyone which commit won, cheaply, and let them fetch it.** A
   member can only pick the right commit if it knows that commit exists. So
   instead of re-sending the whole heavy commit, each holder sends a tiny note
   carrying just its hash ("I have commit X"). That note is light enough to
   reach everyone even under loss, so every member learns which commit is the
   winner and then asks any holder to send that one body. Everyone lands on the
   same commit, and it takes no longer than before (R16: forks go from 285 to 0;
   R17: same 4.7s). This "small note, then fetch the body" step is exactly what
   de-mls is missing today.

3. **The obvious safety rule backfires.** "Don't apply a commit until 2/3 of
   members confirm it" does stop forks — but under loss that confirmation often
   never gathers, so the group freezes instead. At 30% loss it froze on every
   run (0 success), while the note-and-fetch approach succeeded on every run
   (300 success). The confirmation rule only pays off at extreme loss.

**For de-mls:** build the small-note + fetch-the-body step. It is safe as long
as fewer than a third of members are dishonest (R14: a forged commit cannot win
below that line, only above it). Two easy wins come with it — keep the steward
count small (each extra steward adds a full round of traffic, R17), and skip the
longer timer and the strict confirmation rule, since neither adds safety this
step does not already give.

## Files

- `demls_sim.py` — the simulator (all four scenarios, config-driven).
- `test_demls.py` — 18 self-checking requirements; all pass.
- `report.md` — this file.
- `measurements.py` — regenerates every comparison table (Findings 1–4).
- `FINDINGS.md` — the design synthesis: collection, sync, and committee sizing.
- `timing-and-adversary.md` — open notes on silent members and the delivery
  deadline (Δ), and which parameters each affects.

## Running

```bash
# self-check
python3 test_demls.py

# single run
python3 demls_sim.py --plain      --p-loss 0.2 --stewards 3 --members 9  --trials 300
python3 demls_sim.py --reflexion2 --p-loss 0.3 --stewards 3 --members 9  --trials 300
python3 demls_sim.py --committee  --p-loss 0.01 --stewards 3 --members 997 --trials 5

# RFC concurrent minting (all stewards mint at once); add a sync layer on top
python3 demls_sim.py --plain      --all-mint --p-loss 0.2 --stewards 3 --members 9 --trials 300
python3 demls_sim.py --reflexion2 --all-mint --p-loss 0.3 --stewards 3 --members 9 --trials 300

# safety switches
python3 demls_sim.py --reflexion2 --apply-only-on-quorum --p-loss 0.3 --trials 400
python3 demls_sim.py --reflexion2 --equivocate --malicious 0.33 --trials 200
```

Config lives in the `Config` dataclass in `demls_sim.py`, each field commented.
Key ones: `p_loss`, `malicious_ratio`, `committee_ratio`, `bodyreq_grace`,
`suppress_wait`, and the `2n/3` quorum via `quorum_num`/`quorum_den`.
