# Findings: commit collection, sync, and committee sizing

A synthesis of the reliability investigation, with the measurements behind each
claim. Companion to `report.md` (which documents the four base scenarios); this
file is the design story and the numbers. All figures are from `demls_sim.py`
unless marked *(analytic)*.

## The question

Over an unreliable channel some members miss a commit. Does the group converge
on **one** commit (success), diverge (**fork**), or get stuck (**stall**)? A
fork is a silent, unrecoverable split; a stall is recoverable. We want to know
what forks, what fixes it, and what the fix costs.

## Assumptions and model limitations

The numbers below are only as honest as the model. What it does **not** yet
capture:

- **No on-the-fly re-election.** A member that misses the winning commit
  **stalls** (waits); it does not time the steward out, elect a replacement, and
  apply a competing commit. This is why `sn=1` shows **0 forks (only stalls)** —
  a single steward removes the *intra-epoch, multi-candidate* fork, but with
  re-election modeled it could still fork at the **re-election boundary** (the
  timed-out steward applied its own commit while the rest re-elected and applied
  another). So the `sn=1` fork count here is a **lower bound**; the real residual
  risk is the re-election window, removed only by confirm-before-apply.
- **Independent per-link loss** (Bernoulli), not bursty/correlated. A member
  offline for a whole span (losing *every* message) is not modeled; independent
  loss is more optimistic, since redundancy helps more.
- **Single epoch.** No rotation or multi-epoch; every result is within one
  commit round.
- **No MLS crypto.** Only delivery, loss, and the sync logic; a "commit" is an
  opaque id.
- **Honest unless flagged.** Byzantine behavior appears only via the
  `equivocate` / `malicious_ratio` knobs.

## Cost has two independent axes

Do not conflate them.

| axis           | choices                   | what it is                       | message cost    |
| -------------- | ------------------------- | -------------------------------- | --------------- |
| **collection** | primary-first vs all-mint | how stewards produce commits     | O(n) vs O(sn·n) |
| **sync**       | reflexion2 vs committee   | who attests, so members converge | O(n²) vs O(k·n) |

Total = collection + sync + body-pull. **Sync dominates at scale.** Committee is
a *sync* option layered on top of *either* collection model — it does not
replace them.

---

## Finding 1 — the RFC's concurrent minting is what makes forks the default

The original sim is **primary-first**: one epoch steward commits, a backup only
if it missed the primary's commit. The RFC (§Commit validation service) is
**concurrent**: every steward MAY commit at once, and each member locally selects
one winner over the commits it *received* — with no mechanism to make those
received sets equal. `--all-mint` implements the RFC model.

Forks, plain baseline, n=12, same loss:

| p_loss | primary-first forks | all-mint forks |
| -----: | ------------------: | -------------: |
|   0.05 |                  13 |            156 |
|   0.10 |                  36 |            262 |
|   0.20 |                  73 |            351 |
|   0.30 |                 113 |            384 |

Same success rate — they differ only in *how they fail*. Primary-first fails
**safe** (miss the commit → nothing to apply → stall); all-mint fails **unsafe**
(miss the winner → apply a competitor → fork). The original sim understated the
real problem **3–12×**.

The steward count is the dial (all-mint, n=12, p=0.2):

| stewards `sn` |     1 |    2 |    3 |    4 |    6 |
| ------------- | ----: | ---: | ---: | ---: | ---: |
| forks         | **0** |  330 |  351 |  355 |  357 |

`sn=1` shows **0 forks (only stalls)** in this model — but that is *under the
no-re-election assumption* (see Assumptions): it removes the multi-candidate
fork by construction, yet with re-election it could still fork at the
re-election boundary. `sn≥2` flips failures almost entirely to forks.

---

## Finding 2 — a sync layer over the candidate set resolves it

Members converge on the deterministic winner among the candidates they have
heard of: each holder broadcasts a light **attestation of the winner's hash**
(so its *existence* spreads even when the heavy body was dropped), and a member
missing the body **pulls it from any holder**.

all-mint, n=12, success / fork / stall:

| p_loss | plain (no sync) | reflexion2      | committee (k≈2) | apply-only-quorum |
| -----: | --------------- | --------------- | --------------- | ----------------- |
|    0.1 | 99 / 201 / 0    | **300 / 0 / 0** | 270 / 30 / 0    | 246 / 0 / 54      |
|    0.2 | 32 / 262 / 6    | **300 / 0 / 0** | 221 / 79 / 0    | 35 / 0 / 265      |
|    0.3 | 13 / 285 / 2    | **300 / 0 / 0** | 178 / 119 / 3   | 0 / 0 / 300       |
|    0.5 | 0 / 298 / 2     | **300 / 0 / 0** | 42 / 246 / 12   | 0 / 0 / 300       |

- **reflexion2 (everyone attests) fully resolves the fork** at moderate loss
  (0/0 up to p=0.5; degrades only past that). The recovery layer works.
- **apply-only-on-quorum is over-conservative** — 0 forks, but stalls balloon;
  the sync's convergence already makes a provisional apply safe.
- **committee forks here** because at n=12 the committee is ~2 members — the
  small-committee failure (see Finding 4).

---

## Finding 3 — the cost of the RFC model is bandwidth, not time

Holding the sync constant, varying only the collection model (n=12):

| measure                                  | primary-first    | all-mint         |
| ---------------------------------------- | ---------------- | ---------------- |
| plain, msgs                              | 12               | 33               |
| reflexion2 p=0.1 — msgs / conv / success | 158 / 4.7s / 296 | 241 / 4.7s / 300 |
| reflexion2 p=0.2 — msgs / conv / success | 198 / 5.9s / 272 | 264 / 5.9s / 300 |

Commit traffic under all-mint scales linearly with steward count (`sn·(n−1)`);
primary-first stays flat:

| stewards      |    1 |    3 |    6 |
| ------------- | ---: | ---: | ---: |
| primary msgs  |   11 |   12 |   12 |
| all-mint msgs |   11 |   33 |   66 |

**All-mint costs more messages but the same convergence time** (latency is set by
attest+pull rounds, not by how many stewards minted), and the extra broadcasts
**buy robustness** — all-mint reflexion2 reaches 300/300 where primary-first
gets 272, because the winner's body is redundantly held.

Scale check, all-mint + sync, n=1000, sn=3 (all fork-free):

| sync                     | p_loss | success |  msgs | conv |
| ------------------------ | -----: | ------: | ----: | ---: |
| committee (~200 attest)  |   0.01 |     5/5 |  300K | 5.1s |
| committee                |   0.05 |     5/5 |  376K | 5.2s |
| committee                |   0.10 |     5/5 |  476K | 5.2s |
| reflexion2 (1000 attest) |   0.05 |     3/3 | 1.47M | 5.0s |
| reflexion2               |   0.10 |     3/3 | 1.57M | 5.1s |

Committee cuts bandwidth ~3–5× at equal latency and zero forks — the O(k·n) vs
O(n²) saving in real numbers.

---

## Finding 4 — the committee must be sized by a **number**, not a percent

A committee's reliability depends on *how many* members it has, not what
*fraction* of the group. Like a jury: 100 random people are trustworthy whether
the town has 1,000 or 1,000,000. A percent gets both ends wrong — wasteful at
large n, unsafe (2–3 members) at small n. Two independent requirements set the
number.

### 4a. Byzantine safety — `k_safe(f, ε)` *(analytic)*

Minimum committee size so `P(committee ≥ 1/3 Byzantine) ≤ ε`, for a group that
is `f`-fraction Byzantine. n-independent.

| Byz `f` | ε=1e-3 | ε=1e-6 | ε=1e-9 | ε=1e-12 |
| ------: | -----: | -----: | -----: | ------: |
|    0.10 |     22 |     55 |     88 |     121 |
|    0.15 |     43 |    106 |    172 |     235 |
|    0.20 |     97 |    229 |    367 |     505 |
|    0.25 |    271 |    646 |   1030 |    1420 |
|    0.30 |   1828 |   4345 |  >5000 |   >5000 |

It **explodes near 1/3** — committees only make sense when `f` is comfortably
below 1/3; near it, the committee approaches the whole group and there is no
saving (use reflexion2).

### 4b. Why a percent is wasteful *(analytic, f=0.2, fixed k=230)*

`k=230` already gives one-in-a-million safety. 20% just drives the failure
probability to absurd values while paying linear bandwidth for it:

|       n | 20% → k | P_unsafe at 20% | msgs (20%) | msgs (fixed 230) | waste |
| ------: | ------: | --------------: | ---------: | ---------------: | ----: |
|   1,000 |     200 |            5e-6 |       200K |             230K |  0.9× |
|   5,000 |   1,000 |           2e-23 |       5.0M |            1.15M |  4.3× |
|  10,000 |   2,000 |           1e-44 |        20M |             2.3M |  8.7× |
| 100,000 |  20,000 |              ~0 |       2.0B |              23M |   87× |

Safety is already negligible at `k=230`; everything above it is bandwidth spent
shrinking an already-negligible number.

### 4c. Liveness under loss — `k_live(n, p, δ)`

Enough attesters that the winner's hash survives loss and reaches everyone.
Under-size the committee and forks return — forks per 150 trials, n=120,
all-mint committee, honest:

| k \ p |  0.1 |  0.2 |  0.3 |  0.5 |   0.7 |
| ----: | ---: | ---: | ---: | ---: | ----: |
|     2 |   28 |  106 |  146 |  150 |   150 |
|     3 |   39 |   83 |  124 |  150 |   150 |
|     4 |   11 |   27 |   65 |  146 |   150 |
|     6 |    3 |   17 |   25 |   96 |   150 |
|     9 |    0 |    3 |    3 |   28 |   140 |
|    12 |    0 |    1 |    0 |    8 |    97 |
|    18 |    0 |    0 |    0 |    0 |    15 |
|    24 |    0 |    0 |    0 |    0 |     4 |
|    36 |    0 |    0 |    0 |    0 | **0** |

Minimum `k` for zero forks per loss level:

| loss `p` | min k (empirical) | `log(n)/log(1/p)` *(analytic)* |
| -------: | ----------------: | -----------------------------: |
|      0.1 |                 9 |                              3 |
|      0.2 |                18 |                              3 |
|      0.3 |                12 |                              4 |
|      0.5 |                18 |                              7 |
|      0.7 |            **36** |                             14 |

More loss → bigger committee. The scaling law is

```
k_live  ≈  c · log(n / δ) / log(1 / p)
```

where `p` = loss, `n` = group size, `δ` = tolerated fork probability. The shape
matches (grows with loss, log in n); the empirical constant `c ≈ 2–3×` the bound
(independence + union-bound slack).

### 4d. The committee size to pick

```
k = max(  k_safe(f, ε)          ,   c · log(n/δ) / log(1/p)  )
          └ Byzantine (4a) ┘         └ liveness under loss (4c) ┘
```

- **Byzantine** part depends on liar fraction `f` and confidence `ε`; not on loss
  or `n`.
- **Liveness** part depends on loss `p`, size `n`, tolerance `δ`; not on `f`.
- Both are fixed numbers → the percent `k/n` just shrinks as the group grows.
- If `n ≤ k`, skip the committee and let everyone attest.

### 4e. Zero fork tolerance is impossible by sizing

Set `δ → 0` and `k → ∞`: no finite committee gives zero forks, because for any
`k` there is a `p^k > 0` chance every attestation to some member is dropped (the
Two-Generals wall). To *never* fork you must change the rule — **do not apply
until confirmed** (apply-only-on-quorum), which converts forks into stalls. That
is the CAP wall:

```
lossy channel  ⇒  cannot have  (zero forks)  AND  (always live)
```

Pick: stay live and accept a tiny, sized-down fork chance; or never fork and
accept stalls.

---

## What this means for de-mls

- **The fork is a delivery problem the collection model turns into a safety
  problem.** Concurrent minting (the RFC) converts safe stalls into unsafe
  forks; the steward count is the dial (`sn=1` removes the multi-candidate fork
  — only stalls in this model; see Assumptions for the re-election caveat).
- **Build the reliability layer** — light attestation of the winning commit's
  hash + body-pull from any holder. It resolves the RFC fork at equal latency
  and is exactly the primitive de-mls lacks (`resend_commit` is producer-push
  only, `ConversationSync` carries no MLS state).
- **If you use a committee, size it by a number, not a percent** — the max of
  the Byzantine (4a) and liveness (4c) requirements. That keeps it safe at every
  n and makes the sync O(n) instead of O(n²).
- **Do not reach for a bigger inactivity timer or a strict quorum gate** — the
  timer changes nothing under loss, and the quorum gate trades every fork for a
  stall. Only choose the quorum gate when you truly cannot tolerate a fork and
  can tolerate freezing instead.

## Reproduce

```bash
# Finding 1 — collection model forks
python3 demls_sim.py --plain              --p-loss 0.2 --stewards 3 --members 9 --trials 400
python3 demls_sim.py --plain  --all-mint  --p-loss 0.2 --stewards 3 --members 9 --trials 400

# Finding 2 — sync resolves it
python3 demls_sim.py --reflexion2 --all-mint --p-loss 0.3 --stewards 3 --members 9 --trials 300

# Finding 4 — committee sizing curves are computed in test_demls.py (R13) and in
# the analytic snippets kept with this investigation; the loss grid uses
# --committee with committee_ratio = k/n across p_loss.

# Full self-check (R1–R17)
python3 test_demls.py
```
