# Pure SDS Simulation — Report

## Scenario

ES and BS commit at the same time (two concurrent candidates, C-ES and
C-BS). No quorum, no committee — pure SDS: each sender resends its own
commit periodically until acknowledged; participants gossip what they hold
(not just to ES/BS, to everyone); once someone applies, it backs off
instead of going silent; if nothing indicates C-ES exists after a timeout,
fall back to C-BS.

## Why it succeeds when ES is online

- Members only ever apply **C-ES**, never C-BS on first contact — this
  makes forking structurally impossible when ES is present.
- ES resends C-ES until it has real evidence (direct or gossiped) that all
  99 others hold it — not a fixed number of tries, so coverage is
  essentially guaranteed at p_loss ≤ 0.5.
- Result: **100% success, 0 forks, 0 stalls** across p_loss 0.1–0.5.

## Why it "fails" when ES is afk — and why that's misleading

- With no ES, members wait `fallback_timeout` (30s) with zero evidence of
  C-ES, then switch to C-BS. This recovers **~99 of 100** members on
  average (`avg_stall ≈ 1.0`).
- But our success metric is all-or-nothing: even 1 straggler out of 100
  fails the whole trial. So `success_rate = 0%` while the *actual* outcome
  is ~99% individual recovery — the same "weakest link" effect seen
  throughout this project (n independent chances to be unlucky, and only
  one needs to fail).
- Forks stay at 0 either way: the design always degrades to a stall or a
  slow fallback, never to two different applied values.

## Why message count doesn't drop further with more experiments

Two structural reasons, both about **not knowing when to stop**:

1. **Backoff has no stopping condition of its own.** Once settled, a
   participant slows down (20s period) but never goes fully silent — it
   has no way to know "everyone else is also done," so it keeps pinging
   forever at a reduced rate for the rest of `max_time`.
2. **ACKs never resolve to zero traffic.** There's no terminal "ack
   received, stop entirely" state — acknowledgment is inferred
   probabilistically (gossip merges), not a hard confirmation with a
   clear endpoint. ES/BS keep listening and everyone keeps quietly
   reporting, because nothing in the protocol tells any single node when
   the *group* is finished, only when *it personally* is.

This is inherent to gossip-style epidemic protocols: they trade a clean
termination signal for robustness. Tuning `sync_period` /
`settled_sync_period` shifts the cost curve, but doesn't remove this
floor — some steady-state chatter always remains for as long as
`max_time` runs.
