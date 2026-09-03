#!/usr/bin/env python3
"""
Pure SDS (Option A) simulation for de-MLS, per the SDS RFC's base workflow
(no SDS-R / repair extension):

  - ES and BS commit AT THE SAME TIME (t=0), each minting its own candidate
    (C-ES, C-BS) -- two concurrent, independently-legitimate committers.
  - There is no separate ACK message type. Every participant periodically
    broadcasts a light "sync" message revealing what it currently holds
    (its received-set stands in for the RFC's causal_history/bloom_filter --
    we agreed earlier an exact message-id SET does the same job for this
    simulation, no need for a real bloom filter).
  - The ORIGINAL SENDER of each candidate (ES for C-ES, BS for C-BS) is the
    only one who ever rebroadcasts that candidate (Option A: "Periodic
    Outgoing Buffer Sweep" from the RFC). It rebroadcasts on a timer for as
    long as some participants have not yet revealed (via a sync message)
    that they hold it.
  - A participant applies whichever candidate it holds, preferring C-ES
    (the RFC's smallest-committer-id convention used elsewhere in this
    project) if it holds both; it upgrades to C-ES if C-ES arrives after
    it had provisionally applied C-BS. This mirrors the "target only
    improves" logic used in the rest of this session's all_mint work.
  - No repair-request, no response groups, no T_min/T_max backoff pool --
    that machinery belongs to SDS-R, deliberately not used here.

Message accounting is by PUBLISH (one broadcast() call = 1), matching the
pubsub-correct convention established earlier in this session: a publish
reaches everyone in the channel at once, it isn't counted once per
recipient.
"""

import random
import heapq
from dataclasses import dataclass, field


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
@dataclass
class Config:
    n: int = 100                  # total participants (0=ES, 1=BS, rest members)
    p_loss: float = 0.1           # per-delivery drop probability
    delay_min: float = 0.5        # min network delay for a delivered message (s)
    delay_max: float = 2.0        # max network delay for a delivered message (s)
    resend_period: float = 3.0    # ES/BS: how often to check & maybe rebroadcast
    sync_period: float = 3.0      # everyone: how often to broadcast a light sync
    sync_jitter: float = 1.0      # random jitter added to each sync tick, so
                                   # not everyone's sync fires at the exact same
                                   # instant (RFC's "random backoff" suggestion)
    settled_sync_period: float = 20.0  # once a participant has applied C-ES,
                                        # it backs off to this much longer
                                        # period instead of going fully silent
                                        # -- stays a slow-but-available gossip
                                        # source for stragglers, without
                                        # spamming every few seconds forever
    es_afk: bool = False           # if True, ES never commits/broadcasts at
                                    # all -- tests what happens with no
                                    # fallback (members only ever apply C-ES)
    fallback_timeout: float = 30.0  # if by this time there is still NO
                                     # evidence anywhere (direct or via
                                     # gossip) that C-ES exists, fall back
                                     # to applying C-BS instead of waiting
                                     # forever. If evidence exists (someone
                                     # is known to hold C-ES), keep waiting
                                     # for C-ES specifically.
    max_time: float = 60.0        # simulation cutoff


# --------------------------------------------------------------------------
# Event queue plumbing
# --------------------------------------------------------------------------
@dataclass(order=True)
class Event:
    time: float
    seq: int
    kind: str = field(compare=False)
    pid: int = field(compare=False)   # participant this event concerns
    payload: object = field(compare=False, default=None)


class Participant:
    def __init__(self, pid):
        self.id = pid
        self.received = set()      # commit ids this participant holds (bodies)
        self.applied = None        # currently applied commit id
        # Gossip-of-gossip: who I currently BELIEVE holds each candidate,
        # built from my own direct receipt PLUS whatever others' sync
        # messages have told me (merged, epidemic-style, not just "I
        # report directly to ES/BS").
        self.known_holders = {"C-ES": set(), "C-BS": set()}
        self.fallback_active = False  # set True if fallback_timeout expires
                                       # with no evidence C-ES exists


class Simulator:
    def __init__(self, cfg: Config, rng: random.Random):
        self.cfg = cfg
        self.rng = rng
        self.q = []
        self.seq = 0
        self.heavy_publishes = 0   # count of C-ES/C-BS (re)broadcasts
        self.light_publishes = 0   # count of sync-message broadcasts
        self.participants = [Participant(i) for i in range(cfg.n)]
        self.owner_of = {"C-ES": 0, "C-BS": 1}  # who mints/resends each cid

    def _push(self, t, kind, pid, payload=None):
        self.seq += 1
        heapq.heappush(self.q, Event(t, self.seq, kind, pid, payload))

    def _broadcast(self, t, src, cid, heavy):
        """Deliver src's message to every other participant independently,
        each drop decided separately. One publish, regardless of n."""
        if heavy:
            self.heavy_publishes += 1
        else:
            self.light_publishes += 1
        for dst in range(self.cfg.n):
            if dst == src:
                continue
            if self.rng.random() < self.cfg.p_loss:
                continue
            delay = self.rng.uniform(self.cfg.delay_min, self.cfg.delay_max)
            self._push(t + delay, f"DELIVER:{cid}:{'H' if heavy else 'L'}", dst)

    def _broadcast_sync(self, t, src, snapshot):
        """Broadcast a light gossip/sync message carrying a snapshot of the
        sender's known_holders (who it believes holds C-ES / C-BS). Goes to
        EVERYONE, not just ES/BS -- this is what makes it gossip-of-gossip:
        any recipient can learn about holders it never heard from directly."""
        self.light_publishes += 1
        for dst in range(self.cfg.n):
            if dst == src:
                continue
            if self.rng.random() < self.cfg.p_loss:
                continue
            delay = self.rng.uniform(self.cfg.delay_min, self.cfg.delay_max)
            self._push(t + delay, "SYNC_DELIVER", dst, payload=snapshot)

    def _apply_if_better(self, p, cid):
        # Always prefer/apply C-ES the moment it arrives (upgrade even if
        # C-BS was applied earlier via fallback -- the real ES commit, once
        # it shows up, wins).
        if cid == "C-ES":
            p.applied = "C-ES"
        elif cid == "C-BS" and p.fallback_active and p.applied is None:
            # Only apply C-BS if we already gave up waiting for C-ES
            # (fallback_timeout expired with zero evidence C-ES exists).
            p.applied = "C-BS"

    def run(self):
        cfg = self.cfg
        es, bs = self.participants[0], self.participants[1]

        # t=0: ES and BS mint and broadcast their own commit simultaneously
        # -- unless ES is afk, in which case it does nothing at all.
        if not cfg.es_afk:
            es.received.add("C-ES")
            es.applied = "C-ES"
            es.known_holders["C-ES"].add(0)
            self._broadcast(0.0, 0, "C-ES", heavy=True)
        bs.received.add("C-BS")
        bs.known_holders["C-BS"].add(1)
        # BS does NOT self-apply its own C-BS -- it waits for C-ES like
        # everyone else, per the new rule.
        self._broadcast(0.0, 1, "C-BS", heavy=True)

        # Schedule periodic resend checks for ES and BS, and periodic sync
        # broadcasts for every participant (jittered so they don't all
        # fire at the same instant). ES has nothing to resend if afk.
        if not cfg.es_afk:
            self._push(cfg.resend_period, "RESEND:C-ES", 0)
        self._push(cfg.resend_period, "RESEND:C-BS", 1)
        for pid in range(cfg.n):
            jitter = self.rng.uniform(0, cfg.sync_jitter)
            self._push(cfg.sync_period + jitter, "SYNC_TICK", pid)
        # Fallback check: everyone except ES itself evaluates, once, whether
        # to give up waiting for C-ES and switch to C-BS instead.
        for pid in range(cfg.n):
            if pid == 0:
                continue
            self._push(cfg.fallback_timeout, "FALLBACK_CHECK", pid)

        while self.q:
            ev = heapq.heappop(self.q)
            if ev.time > cfg.max_time:
                break
            if ev.kind.startswith("DELIVER:"):
                _, cid, _weight = ev.kind.split(":")
                p = self.participants[ev.pid]
                p.received.add(cid)
                p.known_holders[cid].add(p.id)  # I now know I hold it
                self._apply_if_better(p, cid)
            elif ev.kind == "SYNC_TICK":
                p = self.participants[ev.pid]
                # Broadcast a snapshot of what I currently believe about who
                # holds C-ES/C-BS -- to EVERYONE, not just ES/BS. This is
                # the gossip-of-gossip step: my belief may already include
                # holders I only heard about from someone else's sync, not
                # from them directly.
                snapshot = {cid: set(ids) for cid, ids in p.known_holders.items()}
                self._broadcast_sync(ev.time, p.id, snapshot)
                # Back off to a much longer period once settled (applied),
                # rather than going silent -- still available to gossip to
                # stragglers, just far less often.
                period = cfg.settled_sync_period if p.applied is not None else cfg.sync_period
                jitter = self.rng.uniform(0, cfg.sync_jitter)
                self._push(ev.time + period + jitter, "SYNC_TICK", p.id)
            elif ev.kind == "SYNC_DELIVER":
                p = self.participants[ev.pid]
                snapshot = ev.payload
                for cid, ids in snapshot.items():
                    p.known_holders[cid] |= ids  # merge -- epidemic spread
                # If I now know (via gossip) that C-ES exists and I have its
                # body, or if C-ES itself arrived, _apply_if_better already
                # handles applying; gossip alone (knowing OTHERS have it)
                # does not hand me the body, so no apply check needed here.
            elif ev.kind in ("RESEND:C-ES", "RESEND:C-BS"):
                cid = ev.kind.split(":")[1]
                owner = 0 if cid == "C-ES" else 1
                owner_p = self.participants[owner]
                # Universally known (by everyone else, via direct receipt OR
                # gossip) -> stop resending. Exclude the owner's own id from
                # the count -- it trivially "knows" it has its own commit,
                # that doesn't count as an genuine OTHER participant ack.
                known_others = owner_p.known_holders[cid] - {owner}
                if len(known_others) >= cfg.n - 1:
                    continue
                self._broadcast(ev.time, owner, cid, heavy=True)
                self._push(ev.time + cfg.resend_period, ev.kind, owner)
            elif ev.kind == "FALLBACK_CHECK":
                p = self.participants[ev.pid]
                if p.applied is not None:
                    continue  # already settled on C-ES, nothing to do
                # No evidence anywhere (direct or gossip) that C-ES exists
                # -> give up waiting, switch to C-BS. If evidence DOES
                # exist (someone is known to hold C-ES), keep waiting for
                # it specifically rather than forking to C-BS.
                if len(p.known_holders["C-ES"]) == 0:
                    p.fallback_active = True
                    if "C-BS" in p.received:
                        p.applied = "C-BS"
                    # else: don't have the C-BS body yet either; will apply
                    # it automatically via _apply_if_better whenever it
                    # arrives, now that fallback_active is set.

        return self._result()

    def _result(self):
        applied = [p.applied for p in self.participants]
        distinct = set(a for a in applied if a is not None)
        stalled = sum(1 for a in applied if a is None)
        forked = len(distinct) > 1
        success = (stalled == 0) and (not forked)
        if success:
            status = "SUCCESS"
        elif forked:
            status = "FORK"
        else:
            status = "STALL"
        return {
            "status": status,
            "success": success,
            "forked": forked,
            "stalled_count": stalled,
            "heavy_publishes": self.heavy_publishes,
            "light_publishes": self.light_publishes,
            "total_publishes": self.heavy_publishes + self.light_publishes,
        }


def run_trials(cfg: Config, n_trials: int, seed: int = 0):
    successes = forks = 0
    stall_trials = 0
    total_stalled_members = 0
    heavy_sum = light_sum = 0
    for i in range(n_trials):
        rng = random.Random(seed + i)
        sim = Simulator(cfg, rng)
        res = sim.run()
        if res["success"]:
            successes += 1
        elif res["forked"]:
            forks += 1
        else:
            stall_trials += 1
        total_stalled_members += res["stalled_count"]
        heavy_sum += res["heavy_publishes"]
        light_sum += res["light_publishes"]
    return {
        "trials": n_trials,
        "successes": successes,
        "forks": forks,
        "stall_trials": stall_trials,
        "success_rate": successes / n_trials,
        "avg_stalled_members": total_stalled_members / n_trials,
        "avg_heavy_publishes": heavy_sum / n_trials,
        "avg_light_publishes": light_sum / n_trials,
        "avg_total_publishes": (heavy_sum + light_sum) / n_trials,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Pure SDS (Option A) + gossip "
                                  "+ settle-backoff + BS-fallback simulator")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--trials", type=int, default=50,
                     help="trials per p_loss point (300 is thorough but "
                          "slow -- ~0.4-0.5s/trial at n=100)")
    ap.add_argument("--max-time", type=float, default=60.0)
    ap.add_argument("--es-afk", action="store_true",
                     help="ES never commits at all (tests BS fallback)")
    args = ap.parse_args()

    mode = "ES afk (BS fallback)" if args.es_afk else "ES online"
    print(f"n={args.n}  trials/point={args.trials}  mode={mode}")
    print(f"{'p_loss':>7} {'success':>8} {'avg_stall':>10} {'fork':>6} "
          f"{'heavy_pub':>10} {'light_pub':>10} {'total_pub':>10}")
    for p in [0.1, 0.2, 0.3, 0.4, 0.5]:
        cfg = Config(n=args.n, p_loss=p, max_time=args.max_time,
                     es_afk=args.es_afk)
        out = run_trials(cfg, n_trials=args.trials, seed=5)
        print(f"{p:>7.1f} {out['success_rate']:>8.3f} "
              f"{out['avg_stalled_members']:>10.2f} {out['forks']:>6} "
              f"{out['avg_heavy_publishes']:>10.1f} "
              f"{out['avg_light_publishes']:>10.1f} {out['avg_total_publishes']:>10.1f}")
