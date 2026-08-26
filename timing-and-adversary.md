# Timing (Δ) and adversary model — open notes

Forward-looking notes on two things the sim does **not** model yet — **silent
members** and a **delivery deadline (Δ)** — and, most importantly, a map of
**which parameters each affects and which they leave alone**. The point is to be
able to *predict* the design's behaviour without first measuring the real
network, and to know exactly which one number (Δ) we must measure and where it
enters.

Status: proposed. Nothing here is implemented; `demls_sim.py` currently uses an
independent per-link drop `p_loss` and a delay range that always resolves within
the trial (so delays never cause forks — only drops do).

---

## 1. Silent members ("silent Byzantine" / withholding)

**What it means.** A member that *receives* messages but does not *participate*:
it does not attest, relay, or serve bodies. Present but mute. Distinct from:

- **active Byzantine** — forges / equivocates (our attestation sync already
  resists this: the winner is the smallest real id, a fabricated commit is
  ignored);
- **offline / crashed** — does not receive either;
- **silent** — receives, but stays quiet.

**Real-life correspondence.** Very common — more common than active attacks:
asleep or backgrounded clients, rate-limited nodes, resource-exhausted devices,
lazy implementations, rational free-riders that won't spend bandwidth to attest,
or censorship-by-omission. This is the realistic "adversary" for our layer.

**Parameter introduced:** `silent_ratio` (`s`) — fraction of members (committee
included) that receive but never attest/serve.

**What it affects — liveness only, never safety.** A silent member cannot forge
a commit or push a false winner, so it cannot cause a fork. It only **removes an
attester**. In committee sync the effective committee shrinks:

```
k_effective = k · (1 − s)
```

Silence and loss **compose**: a member learns the winner if *at least one
non-silent committee attestation* reaches it, so

```
P(member misses) ≈ p^{k(1−s)}      ⇒      k_live  ≈  log(n/δ) / [ (1−s) · log(1/p) ]
```

i.e. **silence divides the effective committee** — inflate the liveness size by
`1/(1−s)`. It does **not** touch the Byzantine safety size `k_safe(f, ε)`.

---

## 2. Delivery deadline (Δ)

**What it means.** Every round has a window `W`. A message with delay `> W` is,
for that round, effectively **not delivered** — it counts as loss:

```
p_effective  =  P(dropped)  +  P(delay > W)
                 └ raw loss ┘   └── the Δ term ──┘
```

**Δ must be measured on the real network**  — it is the
delivery-time *distribution*, and its tail is unbounded (a phone in a tunnel, a
GC pause, a relay hiccup). We cannot know it a priori; we can only measure it and
set `W` against it.

**`W` is our timer** (the commit-batch window / deadline). It trades latency for
`p_eff`:

- `W → ∞`: `p_eff → p_drop` (delays always resolve), but latency → ∞.
- `W` small: fast, but `p_eff` picks up the delay tail.
- the tail is unbounded, so `P(delay > W) > 0` always → `p_eff` can never reach
  the raw drop rate (this is the δ=0 impossibility, restated).

So **Δ enters the whole design through exactly two channels**: it sets
`p_eff` (via `W`) and it sets convergence **latency** (∝ Δ). Nothing else.

---

## 3. The map — what each parameter affects

The reason this matters: **almost everything we found is Δ-invariant.** Δ is
confined to two cells. That lets us size and decide most of the design *now*,
and treat Δ as a one-number calibration for the timer plus a latency prediction.

| parameter           |   fork (safety)   | stall / deadlock |  conv time   |  msg cost   | `k_safe` |  `k_live`   |
| ------------------- | :---------------: | :--------------: | :----------: | :---------: | :------: | :---------: |
| `p_loss` (raw drop) |         ✓         |        ✓         | ✓ (retries)  | ✓ (retries) |    —     |      ✓      |
| **Δ (delay dist.)** |    via `p_eff`    |   via `p_eff`    | **✓ direct** |      —      |    —     | via `p_eff` |
| `W` (window/timer)  |    via `p_eff`    |   via `p_eff`    |      ✓       |      —      |    —     | via `p_eff` |
| `f_byz` (active)    | ✓ *(voting only)* |    ✓ (quorum)    |      —       |      —      |    ✓     |      —      |
| `s` (silent)        |         —         |        ✓         |      —       |      —      |    —     |  ✓ (÷ 1−s)  |
| `n` (size)          |      via `k`      |     via `k`      |     ~log     |      ✓      |    —     |   ✓ (log)   |
| `sn` (stewards)     |      ✓ (≥2)       |        —         |      —       | ✓ (O(sn·n)) |    —     |      —      |
| `k` (committee)     |   ✓ (too small)   |        —         |      —       | ✓ (O(k·n))  | *output* |  *output*   |

Reading the two rows that matter most:

- **Δ touches only two cells**: convergence **time** (directly), and **`p_eff`**
  (via the window `W`). It never touches the Byzantine safety size, the winner
  selection, the message *count*, or the mechanism's correctness.
- **`f_byz` only forks a *voting* committee** (de-mls's steward consensus — the
  `k_safe` table's real home). Our *attestation* sync is not a vote, so active
  Byzantine there is a liveness cost, not a fork. (See FINDINGS Q-notes / R18.)

---

## 4. Design consequence — what needs Δ and what does not

**Decidable now, without measuring Δ (Δ-invariant):**

- the collection choice and the `sn` dial (`sn=1` never multi-candidate-forks;
  cost `O(sn·n)`);
- the sync mechanism (attest-hash + body-pull) and its correctness;
- the Byzantine committee size `k_safe(f, ε)` — pure sampling, no time in it;
- message cost / bandwidth budgets (a delay doesn't change the *count*);
- the shape of the liveness law `k_live ∝ log(n/δ)/[(1−s)·log(1/p_eff)]`.

**Needs the real network (measured Δ):**

- the **window/timer `W`** — set so `P(delay > W)` is small *relative to the raw
  drop rate*, then `p_eff ≈ p_drop` and the sizing above holds;
- the **`p_eff`** value to feed into `k_live` (raw drops + the delay tail under
  the chosen `W`);
- the **convergence latency** prediction (∝ Δ × rounds).

So the workflow is: **size and choose everything Δ-free; then measure Δ once to
(a) pick `W`, (b) compute `p_eff`, (c) predict latency.** A timer is a bet on Δ —
it tunes `p_eff`, it does not add safety.

---

## 5. Note on de-mls timers

The timers that map to `W` here are **de-mls's** `ConversationConfig` knobs
(`commit_batch_window`, `freeze_duration`, `consensus_timeout`, `voting_delay`,
recovery windows). This analysis says those timers only move `p_eff` and
latency — never safety — which is consistent with de-mls treating them as
tunable and deliberately refusing Δ-relative sizing (Δ is unknowable). A deeper
audit of the exact current values is likely overkill until we have a measured Δ
to set them against.

---

## 6. If we model it (proposed sim additions)

- `silent_ratio` — mark members receive-but-never-send; measure `k_live` infl
  ation vs `1/(1−s)` and confirm safety is untouched.
- `delivery_deadline` (`W`) — treat `delay > W` as loss; sweep `W/Δ` and watch
  `p_eff` and forks move, turning the timer question into a measured curve.

Both are liveness-side; neither changes the safety results. They would let the
sim *predict* the two Δ-dependent cells above instead of assuming them.
