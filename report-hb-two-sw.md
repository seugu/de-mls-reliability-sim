# Heartbeat Scenario (`hbclose`) — Single and Two Steward — Report

## Part 1 — Single steward, no BS

### Design

One steward (ES) only, no backup steward, so **no fork is possible by
construction**. This isolates a narrower question: given a single
committer, how reliably and how cheaply can everyone converge on its
commit under packet loss, using a heartbeat as a pure existence signal?

- **ES commits** the body at t=0, then **re-pushes the same body**
  periodically (every `hb_interval`) for `hb_window` seconds — proactive,
  not just reactive.
- **ES also heartbeats** on the same cadence: a light, authenticated
  "C-ES exists" signal, carrying no body.
- **Every other participant decides exactly once**, at `t = hb_window`:
  - **(a)** holds the body → apply it now.
  - **(b)** no body, and never saw *any* heartbeat → conclude **ES is afk**.
  - **(c)** no body, but saw ≥1 heartbeat (so the commit provably exists) →
    raise **NO-ES**, staggered by deterministic rank, then pull the body
    via the existing small-pool / cooldown / broadcast-answer mechanism.
- Two ES modes: **commits normally**, or **genuinely afk**.

### Sanity checks

- **ES afk, any n:** 100% of members correctly diagnose "afk" with **zero
  messages sent** — absence of heartbeats over the window is itself the
  signal.
- **Heartbeat removed (body-only ablation):** without heartbeat's existence
  proof, a straggler who misses every periodic body push has no way to know
  a commit exists or to trigger recovery. At n=10,000, p=0.5, 12 body-only
  pushes over ~11s: **0% success** (vs 60% with heartbeat at the same
  loss). Heartbeat's job isn't carrying data — it's telling stragglers
  there's something worth recovering.

### Core result: n=1000, p_loss=0.4, hb_window=10s, 300 trials

```
success = 300/300 (100.0%)
forks   = 0
stalls  = 0
```

### The wall is a "weakest link" effect, not a broken mechanism

A participant misses **all** `k` periodic pushes with probability
`p_loss^k`. Expected stragglers ≈ `n × p_loss^k`. Even a tiny
per-participant miss chance compounds across many participants.

**p_loss = 0.4, by window size, 300 trials each (n=1000):**

| window | success | forks | stalls |
|---:|---:|---:|---:|
| 5s | 77.7% | 0 | 67 |
| 7s | 97.0% | 0 | 9 |
| 10s | **100.0%** | 0 | 0 |

**p_loss = 0.5, n=1000, 300 trials each:**

| window | success | forks | stalls |
|---:|---:|---:|---:|
| 5s | 33.7% | 0 | 199 |
| 7s | 68.0% | 0 | 96 |
| 10s | 95.0% | 0 | 15 |

**p_loss = 0.4, n=10,000** (fewer trials — n=10,000 was too slow for 300
per point in this session, 30–100 used instead; trend is clear, treat
exact percentages as indicative):

| window | trials | success | forks | stalls |
|---:|---:|---:|---:|---:|
| 5s | 30 | 20.0% | 0 | 24 |
| 7s | 100 | 68.0% | 0 | 32 |
| 10s | 100 | 95.0% | 0 | 5 |

Same window, 10x the group: reliability drops, matching `p_loss^k` — more
participants means more chances for one unlucky straggler.

**Every single measurement in Part 1 has zero forks.** The only failure
mode is stalling — a deliberate trade, and the worse outcome is avoided
entirely.

### Cost: publish count (pubsub-correct — a channel publish reaches
everyone at once, not once per recipient), not delivery count

| p_loss | heartbeat publishes | body publishes |
|---:|---:|---:|
| 0.1–0.3 | 10 | 11 |
| 0.4 | 10 | 11–12 |
| 0.5 | 10 | 12–14 |

Heartbeat count is fixed (`hb_window / hb_interval`), independent of loss
and group size. Body count starts at `1 + hb_window/hb_interval` and creeps
up only once loss is high enough that reactive NO-ES/pull also fires.

---

## Part 2 — Two stewards: BS steps in if ES is afk

### Design change

Added a backup steward (BS) with the rule: **if ES is afk, BS's commit is
what gets applied.** Built as a clean two-phase extension of Part 1:

- **Phase 1** (`t = 0..hb_window`): identical to the single-steward design,
  for `C-ES`.
- At `t = hb_window`, anyone who concludes "ES afk" (case b) checks: if
  **that participant is BS itself**, BS steps up exactly the way ES did —
  mints `C-BS`, announces it, and starts its **own** heartbeat+push cycle
  for a second window (`t = hb_window..2×hb_window`).
- Everyone else who also concluded "ES afk" waits, then re-runs the exact
  same 3-way check for `C-BS` at the second window's close (apply / BS also
  unreachable / staggered NO-ES pull for `C-BS`).
- **ES afk mode**: ES sends nothing at all; everyone reaches "ES afk"
  trivially (there's nothing to lose), BS mints C-BS, phase 2 runs its full
  course.

### New failure mode this introduces: BS is a single point of failure for fork

In Part 1, no participant's individual bad luck could cause a fork — the
worst outcome was always a personal stall. Adding BS's "step up if I
conclude ES is afk" role changes that: **if BS itself is unlucky enough to
miss every heartbeat from a genuinely-online ES**, BS will incorrectly
conclude "ES is afk," mint `C-BS`, and start phase 2 — while everyone who
*did* get ES's heartbeats/body correctly applies `C-ES`. Result: a real
fork, even though ES was online the whole time.

This was empirically confirmed, not just theorized:

```
ES online, p=0.5, window=10s (safe), 500 trials: success=1.000 fork=0  stall=0
ES online, p=0.5, window=3s  (short), 500 trials: success=0.680 fork=55 stall=105
```

At `hb_window=10` (10 rounds), BS's own miss-everything probability is
`0.5^10 ≈ 0.1%` — negligible, zero forks across 500 trials. Shrinking the
window to 3 rounds raises BS's individual miss chance to `0.5^3 ≈ 12.5%`,
and **11% of trials fork** (55/500) — purely from BS's own bad luck, not
from any decision by ES. See `bs_fork_risk_stress_test.py`.

**Practical implication:** the window must be long enough that BS's own
`p_loss^rounds` is negligible, same math as everyone else — but unlike a
regular member, BS being wrong here doesn't just strand BS, it can split
the whole group. Size the window for BS's worst-case loss, not the
average case.

### ES online vs ES afk, at the safe window (n=1000, hb_window=10s)

| p_loss | mode | success | fork | stall |
|---:|---|---:|---:|---:|
| 0.1 | online | 100% | 0 | 0 |
| 0.1 | afk | 100% | 0 | 0 |
| 0.3 | online | 100% | 0 | 0 |
| 0.3 | afk | 100% | 0 | 0 |
| 0.5 | online | 92% | 0 | 4 |
| 0.5 | **afk** | **56%** | 0 | 22 |

At low-moderate loss both modes are perfect. At p=0.5, **afk mode is
noticeably weaker** (56% vs 92%). Cause, confirmed by direct log
inspection: in afk mode, *every* member (not just a small fraction) goes
through phase 2's reactive recovery path if they miss BS's periodic
pushes, so the same known residual limitation from Part 1 — a bounded
retry budget (4 attempts) against a small, fixed answer pool (5
participants) — gets exercised by a larger population, and its
already-known non-zero failure rate shows up more often in the aggregate.
This is the same mechanism as Part 1's stalls, not a new bug.

Message cost is close to symmetric between modes (~21–24k at n=1000): in
either case exactly one steward runs a full periodic heartbeat+push cycle
— it's just delayed by one window's length when BS has to step in.

### Bottom line for Part 2

- BS fallback works and, at a properly-sized window, matches Part 1's
  fork-free reliability.
- It reintroduces a real (if narrow) fork surface tied to BS's own luck —
  absent in the single-steward design — that shrinks fast with a longer
  window but is never literally zero.
- ES-afk recovery is measurably weaker than ES-online at high loss, because
  it pushes the *entire* group through the same reactive-recovery path
  that only a few stragglers hit in the online case.

---

## Files

- `demls_sim.py` — simulator, includes both the single-steward (`hbclose`
  with `n_stewards=1`) and two-steward BS-fallback (`hbclose` with
  `n_stewards=2`) logic.
- `n1000_p04_w10_300trials.py` — Part 1's core result (single steward,
  n=1000, p=0.4, window=10s, 300 trials, 100% success).
- `compare_es_modes.py` — Part 2's ES-online-vs-afk comparison table.
- `bs_fork_risk_stress_test.py` — Part 2's empirical confirmation of the
  BS false-afk fork risk at a short window, and its absence at a safe one.
