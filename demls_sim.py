#!/usr/bin/env python3
"""
de-MLS reliability simulator.

We do NOT simulate MLS or de-MLS crypto. We only simulate the RELIABILITY
question: a steward commits, the channel drops messages probabilistically,
and we ask whether the group ends up on ONE agreed commit (success) or forks /
stalls (fail).

Model:
  - No rounds. Every message carries a timestamp and is delivered after a
    random delay (or dropped entirely). Everything is event-driven.
  - One shared channel. Any broadcast (steward or member) is delivered to each
    other participant independently, each delivery dropped with prob p_loss.
  - Single epoch per trial (rotation / multi-epoch is future work).
  - Every participant keeps a per-id log so we can inspect what it experienced.

Scenarios:
  --plain      : steward commits, members apply whatever commit they hold
                 (ES preferred, else BS). No sync mechanism. Expected to fork.
  --reflexion  : members that miss the ES commit broadcast NO-ES; holders
                 re-broadcast the commit; a member that sees the same commit
                 from >= quorum distinct senders is synced. Also classifies
                 type 2 (ES afk) vs type 3 (ES online but network dropped).

Success criterion (global): ALL members end on the exact same commit.
Anyone who applied a different commit (fork) OR applied nothing (stall) => fail.
"""

import argparse
import hashlib
import heapq
import random
from dataclasses import dataclass, field


# --------------------------------------------------------------------------
# Config. Every field has a simple inline comment. Values here are defaults;
# any of them can be overridden from the command line.
# --------------------------------------------------------------------------
@dataclass
class Config:
    n_stewards: int = 3          # number of stewards (they can produce commits)
    n_members: int = 9           # number of plain members (apply commits only)
    p_loss: float = 0.1          # per-delivery drop probability (0..1)
    malicious_ratio: float = 0.0 # fraction of participants that are malicious
    delay_min: float = 0.5       # min network delay for a delivered message (s)
    delay_max: float = 2.0       # max network delay for a delivered message (s)
    noes_wait: float = 3.0       # how long a member waits before sending NO-ES (s)
    quorum_num: int = 2          # quorum numerator   -> quorum = ceil(num/den * n)
    quorum_den: int = 3          # quorum denominator -> default 2n/3
    max_time: float = 60.0       # stop the event loop at this sim time (s)
    commit_bytes: int = 4096     # size of a heavy COMMIT body message (bytes)
    attest_bytes: int = 96       # size of a light NO-ES / ATTEST message (bytes)
    suppress_wait: float = 0.3   # random wait before attesting; suppress if quorum met
    bodyreq_retry: float = 4.0   # if body not received, re-request after this (s)
    bodyreq_grace: float = 2.5   # wait this long after seeing an attest before
                                 # requesting the body (it may be in flight)
    max_bodyreq: int = 4         # max body-request attempts before giving up
    committee_ratio: float = 0.2 # fraction forming the control committee
    sds_rebroadcast: float = 3.0 # SDS: re-broadcast unacked messages after this (s)
    sds_ack_wait: float = 1.0    # SDS: how long a receiver waits before ACKing
                                 # (batches acks; real SDS acks via causal_history
                                 # of its own next message, approximated here)
    hb_interval: float = 1.0    # heartbeat: ES re-announces existence this often
    hb_window: float = 10.0     # heartbeat: keep announcing for this long total
    request_pool_size: int = 5  # only this many lowest-rank participants get
                                 # individually staggered request delays;
                                 # everyone else waits for the pool's turn
    answer_pool_size: int = 5   # same idea, for who is even eligible to answer
    request_stagger: float = 1.0  # extra delay per rank step, requester side
    answer_stagger: float = 2.0   # extra delay per rank step, answerer side
                                   # (>= delay_max so an earlier rank's
                                   # broadcast answer has time to arrive
                                   # before a later rank's turn comes up)
    answer_cooldown: float = 4.0  # after answering (or cancelling), ignore
                                   # further requests for this cid for this
                                   # long -- otherwise every new incoming
                                   # request (there can be many, from many
                                   # distinct stragglers) re-triggers us,
                                   # since we can't see our own broadcast
    n_trials: int = 500          # random trials per run
    seed: int = 0                # base RNG seed (trial i uses seed+i)
    scenario: str = "plain"      # plain|reflexion|reflexion2|committee|sds|hybrid|heartbeat
    apply_only_on_quorum: bool = False  # if True, never apply a commit before
                                        # body+quorum both hold (0 forks, but
                                        # a lost quorum becomes a stall)
    equivocate: bool = False    # malicious participants attest+serve a forged
                                # "C-EVIL" commit instead of staying silent
    all_mint: bool = False      # every steward mints its own commit at t=0
                                # (RFC's actual "multiple stewards MAY commit"
                                # behavior), instead of only the ES committing
                                # and a backup stepping in on failure


def quorum_size(cfg: Config, n: int) -> int:
    # ceil(num/den * n), the "2n/3 distinct senders" threshold
    num, den = cfg.quorum_num, cfg.quorum_den
    return -(-(num * n) // den)


# --------------------------------------------------------------------------
# Messages and events
# --------------------------------------------------------------------------
COMMIT = "COMMIT"   # heavy: carries commit body (commit_id + committer)
NOES = "NO-ES"      # light: "I don't have the ES commit"
ATTEST = "ATTEST"   # light: "I have seen commit X" (hash only, for quorum)
BODYREQ = "BODYREQ" # light: "send me the body of commit X" (type3 pull)
ACK = "ACK"         # light: SDS acknowledgement of a received message
HEARTBEAT = "HEARTBEAT"  # light: authenticated "I am ES and I committed cid"

@dataclass(order=True)
class Event:
    time: float
    seq: int                       # tiebreaker so equal timestamps are stable
    kind: str = field(compare=False)
    src: int = field(compare=False)
    dst: int = field(compare=False)
    payload: dict = field(compare=False)


# --------------------------------------------------------------------------
# Participant
# --------------------------------------------------------------------------
class Participant:
    def __init__(self, pid, role, malicious):
        self.id = pid
        self.role = role              # "ES", "BS", "steward", "member"
        self.malicious = malicious
        self.applied_commit = None    # the commit id this participant settled on
        self.synced = False
        self.diagnosis = None         # "got-es", "type2-afk", "type3-netfail"
        # commit_id -> set of distinct senders we heard it from
        self.commit_sources = {}
        self.seen_noes = set()        # ids of members we saw NO-ES from
        self.sent_own_commit = False  # BS: did we already emit our own commit?
        self.answered_noes = False    # did we already answer a NO-ES?
        # reflexion2 (attestation-based) state:
        self.attest_sources = {}      # commit_id -> set of ids that attested it
        self.has_body = set()         # commit_ids whose full body we hold
        self.attested = False         # did we already send our own attestation?
        self.body_requested = set()   # cids we've already sent a body request for
        self.bodyreq_attempts = {}    # cid -> how many body requests sent for it
        # SDS scenario state:
        self.seen_ids = set()         # message ids we have seen (our "log")
        self.acked = False            # did we already ack the message we hold?
        self.confirmed_exists = set() # commit ids confirmed via heartbeat
        self.bodyreq_pending = set()  # cids with a currently-scheduled answer check
        self.answer_cooldown_until = {}  # cid -> sim time we can next consider answering
        self.log = []                 # (time, text) history for inspection

    def note(self, t, text):
        self.log.append((round(t, 3), text))


# --------------------------------------------------------------------------
# Simulator
# --------------------------------------------------------------------------
class Simulator:
    def __init__(self, cfg: Config, rng: random.Random):
        self.cfg = cfg
        self.rng = rng
        self.q = []                   # event heap
        self.seq = 0
        self.msg_count = 0            # every broadcast delivery attempt (drops incl.)
        self.byte_count = 0           # total bytes put on the wire (drops incl.)
        self.participants = {}
        self.n = cfg.n_stewards + cfg.n_members
        self.quorum = quorum_size(cfg, self.n)
        self.epoch = 5                # single-epoch sim; fixed value for hashing
        self.committee = self._select_committee()
        # committee quorum = ceil(2/3 of committee size)
        self.committee_quorum = -(-(2 * len(self.committee)) // 3) if self.committee else 0
        self.sds_acked = set()        # SDS: participant ids that ACKed C-ES so far
        self.rank_of = self._compute_rank()  # deterministic id -> rank (0 = first)
        self._build_participants()

    def _select_committee(self):
        # Deterministic committee: sort members by SHA256(epoch || member_id),
        # take the smallest committee_ratio fraction. Everyone can recompute it.
        k = int(round(self.cfg.committee_ratio * self.n))
        if k <= 0:
            return set()
        def h(pid):
            return hashlib.sha256(f"{self.epoch}|{pid}".encode()).hexdigest()
        ordered = sorted(range(self.n), key=h)
        return set(ordered[:k])

    def _compute_rank(self):
        # Deterministic total order over all n participants, same style as
        # committee selection, but a different hash namespace ("rank" tag) so
        # committee membership and bodyreq-answer rank are independent draws.
        def h(pid):
            return hashlib.sha256(f"{self.epoch}|rank|{pid}".encode()).hexdigest()
        ordered = sorted(range(self.n), key=h)
        return {pid: i for i, pid in enumerate(ordered)}

    def _stagger_delay(self, pid, base_stagger, pool_size):
        # Rank 0..pool_size-1 get individually staggered delays (0, base,
        # 2*base, ...). Everyone else waits for the whole pool's turn, then
        # would act together if still needed (their own has_body/has_answered
        # check filters out most of them by then).
        r = self.rank_of[pid]
        return min(r, pool_size) * base_stagger

    def _build_participants(self):
        ids = list(range(self.n))
        # decide malicious set
        n_mal = int(round(self.cfg.malicious_ratio * self.n))
        mal = set(self.rng.sample(ids, n_mal)) if n_mal else set()
        # first n_stewards ids are stewards; index 0 = ES, index 1 = BS
        for pid in ids:
            if pid == 0:
                role = "ES"
            elif pid == 1 and self.cfg.n_stewards >= 2:
                role = "BS"
            elif pid < self.cfg.n_stewards:
                role = "steward"
            else:
                role = "member"
            self.participants[pid] = Participant(pid, role, pid in mal)

    # ---- event plumbing -------------------------------------------------
    def _push(self, time, kind, src, dst, payload):
        self.seq += 1
        heapq.heappush(self.q, Event(time, self.seq, kind, src, dst, payload))

    def unicast(self, t, src, dst, kind, payload):
        """Send to a single destination (targeted). Still subject to p_loss.
        Used for body responses so the heavy COMMIT goes only to the requester,
        not the whole channel."""
        size = self._msg_size(kind)
        self.msg_count += 1
        self.byte_count += size
        if self.rng.random() < self.cfg.p_loss:
            return
        delay = self.rng.uniform(self.cfg.delay_min, self.cfg.delay_max)
        self._push(t + delay, kind, src, dst, payload)

    def _msg_size(self, kind):
        # heavy body vs light control messages
        if kind == COMMIT:
            return self.cfg.commit_bytes
        return self.cfg.attest_bytes

    def broadcast(self, t, src, kind, payload):
        """Send to every other participant; each link dropped independently.
        Every delivery attempt counts as one network message (drops included).
        Bytes are counted per attempt too, using the message kind's size."""
        size = self._msg_size(kind)
        for dst in self.participants:
            if dst == src:
                continue
            self.msg_count += 1
            self.byte_count += size
            if self.rng.random() < self.cfg.p_loss:
                continue  # dropped, never delivered
            delay = self.rng.uniform(self.cfg.delay_min, self.cfg.delay_max)
            self._push(t + delay, kind, src, dst, payload)

    # ---- delivery handlers ---------------------------------------------
    def _deliver_commit(self, ev):
        p = self.participants[ev.dst]
        cid = ev.payload["commit_id"]
        sender = ev.src
        srcs = p.commit_sources.setdefault(cid, set())
        srcs.add(sender)
        p.has_body.add(cid)  # receiving a COMMIT means we now hold its body
        p.note(ev.time, f"recv COMMIT {cid} from {sender} (distinct={len(srcs)})")

        if self.cfg.scenario == "plain":
            self._plain_on_commit(p, ev)
        elif self.cfg.scenario == "reflexion":
            self._reflexion_on_commit(p, ev)
        elif self.cfg.scenario == "sds":
            self._sds_on_commit(p, ev)
        elif self.cfg.scenario in ("heartbeat", "hbclose"):
            self._hb_on_commit(p, ev)
        else:  # reflexion2 or committee
            self._reflexion2_on_commit(p, ev)

    def _deliver_noes(self, ev):
        p = self.participants[ev.dst]
        p.seen_noes.add(ev.src)
        p.note(ev.time, f"recv NO-ES from {ev.src}")
        if self.cfg.scenario == "reflexion2":
            self._deliver_noes_r2(p, ev.time)
            return
        if self.cfg.scenario != "reflexion":
            return
        held = self._held_commit(p)
        if held is None:
            return
        # A holder answers NO-ES by re-broadcasting its commit AT MOST ONCE.
        # This mirrors real dedup and keeps the message count bounded.
        if getattr(p, "answered_noes", False):
            return
        if p.malicious:
            p.note(ev.time, "malicious: has commit but stays silent to NO-ES")
            p.answered_noes = True
            return
        p.answered_noes = True
        self.broadcast(ev.time, p.id, COMMIT, {"commit_id": held})
        p.note(ev.time, f"answer NO-ES: rebroadcast COMMIT {held}")

    # ---- plain scenario -------------------------------------------------
    def _plain_on_commit(self, p, ev):
        # Apply ES commit if we have it; otherwise fall back to BS commit.
        # "Apply" here = settle immediately on the best commit we currently hold.
        best = self._held_commit(p)
        if best is not None and p.applied_commit != best:
            p.applied_commit = best
            p.note(ev.time, f"apply commit {best}")

    def _held_commit(self, p):
        """Best commit a participant holds. Under all_mint, every steward
        mints its own C-S{sid}; the RFC's rule (prefer epoch steward, else
        smallest committer id) reduces to "prefer C-S0, else the smallest
        steward id known" since steward ids are 0..n_stewards-1. Otherwise,
        prefer the single ES's commit, else BS's, else anything held."""
        if self.cfg.all_mint:
            held = [c for c in p.commit_sources if c.startswith("C-S")]
            if not held:
                return None
            if "C-S0" in held:
                return "C-S0"
            return min(held, key=lambda c: int(c[3:]))
        es_cid = "C-ES"
        bs_cid = "C-BS"
        if es_cid in p.commit_sources:
            return es_cid
        if bs_cid in p.commit_sources:
            return bs_cid
        # any other commit (e.g. malicious)
        for cid in p.commit_sources:
            return cid
        return None

    # ---- reflexion scenario --------------------------------------------
    def _reflexion_on_commit(self, p, ev):
        cid = ev.payload["commit_id"]
        srcs = p.commit_sources[cid]
        # If we now have the ES commit directly/enough, settle on it.
        if cid == "C-ES" and not p.synced:
            if len(srcs) >= 1 and p.applied_commit is None:
                # we do have the ES commit; provisional apply
                p.applied_commit = "C-ES"
                p.diagnosis = "got-es"
                p.note(ev.time, "have ES commit -> apply C-ES")
        # Quorum of DISTINCT senders confirming the same commit => synced.
        if len(srcs) >= self.quorum and not p.synced:
            p.synced = True
            p.applied_commit = cid
            if p.diagnosis != "got-es":
                p.diagnosis = "type3-netfail"  # saw quorum but not from ES first
            p.note(ev.time,
                   f"SYNCED on {cid} via {len(srcs)} distinct (quorum={self.quorum})")

    # ---- NO-ES trigger --------------------------------------------------
    def _noes_check(self, ev):
        p = self.participants[ev.dst]
        # Fired at noes_wait. If we still lack the ES commit, announce NO-ES.
        if "C-ES" in p.commit_sources:
            return  # got it in time, nothing to do
        # Malicious "almadım diyip aldım" is handled below by inverting.
        if p.malicious and self._held_commit(p) is not None:
            # has a commit but lies that it doesn't -> still sends NO-ES (noise)
            p.note(ev.time, "malicious: lies NO-ES despite holding a commit")
        p.note(ev.time, "no ES commit by deadline -> broadcast NO-ES")
        self.broadcast(ev.time, p.id, NOES, {})
        # BS special case: if BS has no ES commit, it emits its OWN commit.
        if p.role == "BS" and not p.sent_own_commit and "C-ES" not in p.commit_sources:
            p.sent_own_commit = True
            self.broadcast(ev.time, p.id, COMMIT, {"commit_id": "C-BS"})
            p.note(ev.time, "BS emits own commit C-BS")

    def _bs_deadline(self, ev):
        # Plain scenario: BS emits its own commit if it never got the ES one.
        # BS cannot tell afk from network drop, just like any member -- UNLESS
        # heartbeat scenario is active and BS already confirmed C-ES exists via
        # a heartbeat, in which case BS must not fork onto its own commit; it
        # pulls the body instead.
        p = self.participants[ev.dst]
        if p.role != "BS":
            return
        if "C-ES" in p.commit_sources:
            return  # saw ES commit, no need to emit own
        if self.cfg.scenario == "heartbeat" and "C-ES" in p.confirmed_exists:
            p.note(ev.time, "BS deadline: heartbeat confirmed C-ES exists, "
                             "pulling body instead of forking")
            if "C-ES" not in p.body_requested:
                p.body_requested.add("C-ES")
                self._send_bodyreq(p, ev.time, "C-ES")
            return
        if p.sent_own_commit:
            return
        p.sent_own_commit = True
        self.broadcast(ev.time, p.id, COMMIT, {"commit_id": "C-BS"})
        p.applied_commit = "C-BS"
        p.note(ev.time, "BS deadline: no ES commit, no heartbeat -> emit + apply C-BS")

    # ---- heartbeat scenario: ES self-announces existence, cheaply and often
    # A member lacking the body but seeing >=1 authenticated heartbeat knows
    # for certain the commit exists, and pulls it instead of ever concluding
    # "ES is afk" -- this is what prevents BS's fork-causing fallback.
    def _hb_on_commit(self, p, ev):
        cid = ev.payload["commit_id"]
        if p.applied_commit is None:
            p.applied_commit = cid
            p.diagnosis = "got-es" if cid == "C-ES" else "got-bs"
            p.note(ev.time, f"heartbeat scenario: have body -> apply {cid}")

    def _deliver_heartbeat(self, ev):
        p = self.participants[ev.dst]
        cid = ev.payload["commit_id"]
        p.confirmed_exists.add(cid)
        p.note(ev.time, f"recv HEARTBEAT for {cid} (from {ev.src})")
        if self.cfg.scenario == "hbclose":
            # hbclose deliberately does NOT react eagerly to each heartbeat --
            # it only records that the commit exists. All decisions (apply /
            # afk / staggered NO-ES pull) happen once, at the window close.
            return
        if cid not in p.has_body and cid not in p.body_requested and p.applied_commit is None:
            p.body_requested.add(cid)
            p.diagnosis = "type3-netfail"
            stagger = self._stagger_delay(p.id, self.cfg.request_stagger,
                                          self.cfg.request_pool_size)
            self._push(ev.time + self.cfg.bodyreq_grace + stagger, "BODYREQ_GRACE",
                       p.id, p.id, {"commit_id": cid})

    # ---- hbclose scenario: single steward, heartbeat-only existence signal,
    # all decisions made exactly once at the hb_window deadline.
    #
    # With a BS present (n_stewards >= 2), this becomes two-phase: phase 1
    # is exactly as before, for C-ES. If a participant concludes ES is afk
    # (no heartbeat seen for the whole window), then:
    #   - if THAT participant is BS itself, it steps up: mints C-BS and
    #     starts its own heartbeat+push cycle for a second window.
    #   - everyone else who also concluded ES-afk waits for BS's phase and
    #     re-runs the exact same check for C-BS at the second window's close.
    # Fork-safety note: this assumes BS's own individual heartbeat-miss
    # probability over hb_window/hb_interval rounds is low enough that BS
    # essentially never falsely concludes "ES afk" while ES is genuinely
    # online (same p_loss^rounds tail as the rest of the design) -- but
    # unlike a regular member's bad luck (which only strands that member),
    # BS being wrong here DOES create a real, if rare, fork surface.
    def _hbclose_check(self, ev):
        p = self.participants[ev.dst]
        cid = ev.payload.get("commit_id", "C-ES")
        if cid in p.has_body:
            if p.applied_commit is None:
                p.applied_commit = cid
                p.diagnosis = "got-es" if cid == "C-ES" else "got-bs"
                p.note(ev.time, f"window closed: have {cid} body -> apply")
            return
        if cid not in p.confirmed_exists:
            if cid == "C-ES":
                p.diagnosis = "type2-afk"
                p.note(ev.time, "window closed: no body, no heartbeat ever "
                                 "seen -> conclude ES afk")
                if p.role == "BS":
                    self._hbclose_bs_mint(p, ev.time)
                elif self.cfg.n_stewards >= 2:
                    # wait for BS's phase-2 window, then re-run this same
                    # check for C-BS
                    self._push(ev.time + self.cfg.hb_window + self.cfg.hb_interval,
                               "HBCLOSE_CHECK", p.id, p.id, {"commit_id": "C-BS"})
            else:
                p.diagnosis = "type2-afk-bs"
                p.note(ev.time, "window closed: BS also unreachable, giving up")
            return
        # We know (via heartbeat) the commit exists, but lack the body.
        # Raise NO-ES, staggered deterministically by rank so not everyone
        # who's in this situation broadcasts at the same instant.
        p.diagnosis = "type3-netfail"
        stagger = self._stagger_delay(p.id, self.cfg.request_stagger,
                                      self.cfg.request_pool_size)
        self._push(ev.time + stagger, "HBCLOSE_NOES", p.id, p.id,
                   {"commit_id": cid})

    def _hbclose_bs_mint(self, p, t):
        # BS concludes (from its own observation) that ES is afk, and steps
        # up exactly the way ES would have: mints its own commit, announces
        # it, and starts a second heartbeat+push cycle for it.
        cid = "C-BS"
        p.commit_sources.setdefault(cid, set()).add(p.id)
        p.has_body.add(cid)
        p.applied_commit = cid
        p.diagnosis = "got-bs"
        self.broadcast(t, p.id, COMMIT, {"commit_id": cid})
        p.note(t, "BS concludes ES afk -> mints C-BS, starts phase 2")
        self._push(t + self.cfg.hb_interval, "HB_CHECK_BS", p.id, p.id, {})

    def _bs_heartbeat_check(self, ev):
        # Mirrors _es_heartbeat_check, but for BS/C-BS during phase 2
        # (t = hb_window .. 2*hb_window).
        bs = self.participants[ev.dst]
        if ev.time > 2 * self.cfg.hb_window:
            return
        self.broadcast(ev.time, bs.id, HEARTBEAT, {"commit_id": "C-BS"})
        self.broadcast(ev.time, bs.id, COMMIT, {"commit_id": "C-BS"})
        bs.note(ev.time, "BS heartbeat: announce+push C-BS")
        self._push(ev.time + self.cfg.hb_interval, "HB_CHECK_BS", bs.id, bs.id, {})

    def _hbclose_noes(self, ev):
        p = self.participants[ev.dst]
        cid = ev.payload["commit_id"]
        if cid in p.has_body or p.synced or cid in p.body_requested:
            return  # got it meanwhile, or already pulling
        p.body_requested.add(cid)
        p.note(ev.time, "window closed: raising NO-ES (staggered by rank)")
        self.broadcast(ev.time, p.id, NOES, {})  # the flag itself (for visibility)
        self._send_bodyreq(p, ev.time, cid)       # the actual efficient pull

    def _es_heartbeat_check(self, ev):
        # Fires every hb_interval seconds for hb_window total. Cheap, light,
        # authenticated (src is the real ES id in this model). If ES is
        # genuinely afk, it never heartbeats at all -- this must actually be
        # checked, or "ES afk" trials would falsely still show heartbeats.
        if getattr(self.cfg, "_es_afk", False):
            return
        es = self.participants[0]
        if ev.time > self.cfg.hb_window:
            return
        self.broadcast(ev.time, es.id, HEARTBEAT, {"commit_id": "C-ES"})
        es.note(ev.time, "heartbeat: announce C-ES exists")
        if self.cfg.scenario == "hbclose":
            # Also periodically re-push the actual (heavy) body, not just
            # the light existence signal. This gives per-participant
            # reliability many independent chances to receive the body
            # directly, instead of depending solely on reactive recovery
            # after the window closes -- P(miss all pushes) = p_loss^rounds,
            # which collapses fast even under heavy loss.
            self.broadcast(ev.time, es.id, COMMIT, {"commit_id": "C-ES"})
            es.note(ev.time, "heartbeat: also re-push C-ES body")
        self._push(ev.time + self.cfg.hb_interval, "HB_CHECK", es.id, es.id, {})

    # ---- SDS scenario (periodic rebroadcast + ack, per vac/raw/sds.md) --
    # Simplified per the spec's core mechanism: no bloom filter (a plain set
    # of seen message ids does the same job for our purposes), no causal
    # history chain (single message per trial). What we DO keep faithfully:
    # the sender rebroadcasts an unacknowledged message after a fixed period,
    # and this repeats until every participant has acked or the deadline hits.
    def _sds_on_commit(self, p, ev):
        cid = ev.payload["commit_id"]
        p.seen_ids.add(cid)
        if p.applied_commit is None:
            p.applied_commit = cid
            p.diagnosis = "got-es"
            p.note(ev.time, f"SDS: received {cid}, applied")
        if not p.acked:
            p.acked = True
            self.unicast(ev.time, p.id, ev.src, ACK, {"commit_id": cid})
            p.note(ev.time, f"SDS: ACK {cid} -> {ev.src}")

    def _deliver_ack(self, ev):
        # Only the ES (the original sender) tracks acks in this simplified
        # single-message model.
        if ev.dst != 0:
            return
        self.sds_acked.add(ev.src)
        self.participants[0].note(ev.time, f"SDS: recv ACK from {ev.src} "
                                            f"(acked={len(self.sds_acked)}/{self.n-1})")

    def _sds_rebroadcast_check(self, ev):
        # Fires every sds_rebroadcast seconds. If not everyone has acked yet,
        # the sender re-broadcasts the (heavy) message to the whole channel --
        # SDS has no targeted retransmission without store nodes, so this is
        # a full rebroadcast, matching "MUST rebroadcast unacknowledged
        # outgoing messages after a set period."
        es = self.participants[0]
        if len(self.sds_acked) >= self.n - 1:
            return  # everyone acked, nothing to do
        self.broadcast(ev.time, es.id, COMMIT, {"commit_id": "C-ES"})
        es.note(ev.time, f"SDS: rebroadcast C-ES (acked so far={len(self.sds_acked)})")
        self._push(ev.time + self.cfg.sds_rebroadcast, "SDS_REBROADCAST",
                   es.id, es.id, {})

    # ---- hybrid scenario: committee decision + SDS-style push delivery --
    # Decision (who won) uses the committee's cheap, Byzantine-safe
    # attestation, exactly like --committee. Delivery of the winning BODY
    # does not wait on the reactive grace+bodyreq chain; instead the ES
    # proactively re-pushes it every sds_rebroadcast seconds (no artificial
    # delay), which is what let plain SDS converge faster under tight
    # deadlines. This keeps committee's decision safety and SDS's delivery
    # speed, without paying reflexion2's O(n^2) attestation cost.
    def _hybrid_push_check(self, ev):
        have = sum(1 for p in self.participants.values() if "C-ES" in p.has_body)
        if have >= self.n:
            return  # everyone already has the body, stop pushing
        es = self.participants[0]
        self.broadcast(ev.time, es.id, COMMIT, {"commit_id": "C-ES"})
        es.note(ev.time, f"hybrid: push C-ES (have body={have}/{self.n})")
        self._push(ev.time + self.cfg.sds_rebroadcast, "HYBRID_PUSH", es.id, es.id, {})

    # ---- reflexion2 / committee shared helpers -------------------------
    def _eff_quorum(self):
        # committee/hybrid use the smaller committee-internal 2/3 threshold;
        # reflexion2 uses the global 2n/3.
        if self.cfg.scenario in ("committee", "hybrid"):
            return self.committee_quorum
        return self.quorum

    def _can_attest(self, p):
        # In committee/hybrid mode only committee members attest; otherwise
        # anyone with the body may attest.
        if self.cfg.scenario in ("committee", "hybrid"):
            return p.id in self.committee
        return True

    def _target_commit(self, p):
        """Which commit id a participant provisionally prefers, absent a
        confirmed quorum. Under all_mint, multiple stewards may each mint
        their own candidate; the target is whichever is RFC-preferred (see
        _held_commit) among everything observed so far (via body or
        attestation). This can only move to a smaller (more-preferred) id
        as more is learned, never upward -- so it never oscillates."""
        if not self.cfg.all_mint:
            return "C-ES"
        known = set(p.commit_sources) | set(p.attest_sources)
        known = [c for c in known if c.startswith("C-S")]
        if not known:
            return None
        return min(known, key=lambda c: int(c[3:]))

    # ---- reflexion2 scenario (attestation + body pull) -----------------
    def _reflexion2_on_commit(self, p, ev):
        # Provisional apply/upgrade: re-evaluate our preferred target every
        # time new evidence arrives. Under all_mint the target can only ever
        # become MORE preferred (a smaller steward id) as more is learned, so
        # this "upgrades" rather than oscillates. apply_only_on_quorum=True
        # disables this opportunistic path entirely (0 forks, but a lost
        # quorum becomes a stall -- see R12).
        cid = ev.payload["commit_id"]
        target = self._target_commit(p)
        if (not self.cfg.apply_only_on_quorum and not p.synced and target is not None
                and target in p.has_body and p.applied_commit != target):
            p.applied_commit = target
            p.diagnosis = "got-es"
            p.note(ev.time, f"have {target} body -> provisional apply/upgrade")
        # Quorum-confirmed sync. Under equivocate we deliberately accept ANY
        # cid that reaches quorum (that's the vulnerability R14 tests: a
        # forged commit isn't the preferred target, it just needs enough
        # liars). Otherwise (legitimate multi-committer races, e.g.
        # all_mint), we require the syncing cid to be our current preferred
        # target too, so a less-preferred candidate winning a quorum race
        # doesn't lock in over one we know is more preferred.
        srcs = p.attest_sources.get(cid, set())
        if (cid in p.has_body and len(srcs) >= self._eff_quorum() and not p.synced
                and (self.cfg.equivocate or cid == target)):
            p.synced = True
            p.applied_commit = cid
            if p.diagnosis != "got-es":
                p.diagnosis = "type3-netfail"
            p.note(ev.time, f"SYNCED on {cid} (body arrived, quorum held)")
        # We hold a body, so we can attest it (light message).
        self._maybe_attest(p, ev.time, cid)

    def _maybe_attest(self, p, t, cid):
        # Send a light ATTEST once, after a short suppression wait.
        if p.attested or p.malicious or not self._can_attest(p):
            return
        # schedule a suppression check: if quorum already reached by then, skip
        self._push(t + self.cfg.suppress_wait, "ATTEST_CHECK", p.id, p.id,
                   {"commit_id": cid})

    def _attest_check(self, ev):
        p = self.participants[ev.dst]
        cid = ev.payload["commit_id"]
        if p.attested:
            return
        # Suppression: if we already see quorum attestations, don't add noise.
        srcs = p.attest_sources.get(cid, set())
        if len(srcs) >= self._eff_quorum():
            p.note(ev.time, "suppressed own ATTEST (quorum already seen)")
            p.attested = True
            return
        p.attested = True
        self.broadcast(ev.time, p.id, ATTEST, {"commit_id": cid})
        p.note(ev.time, f"broadcast ATTEST {cid}")

    def _deliver_attest(self, ev):
        p = self.participants[ev.dst]
        cid = ev.payload["commit_id"]
        srcs = p.attest_sources.setdefault(cid, set())
        srcs.add(ev.src)
        p.note(ev.time, f"recv ATTEST {cid} from {ev.src} (distinct={len(srcs)})")

        # If a commit is being attested but we lack its body, we will pull it,
        # but not immediately: the body may still be in flight. Schedule a grace
        # check; only request if the body still hasn't arrived by then.
        if cid not in p.has_body and cid not in p.body_requested and not p.synced:
            p.body_requested.add(cid)  # mark intent so we schedule only once per cid
            p.diagnosis = "type3-netfail"
            self._push(ev.time + self.cfg.bodyreq_grace, "BODYREQ_GRACE",
                       p.id, p.id, {"commit_id": cid})

        # Quorum of distinct attestations => the commit provably exists.
        # Sync completes only once we ALSO hold the body. Same target-vs-
        # equivocate gating as _reflexion2_on_commit: under legitimate
        # multi-committer races (all_mint) we require cid to be our current
        # preferred target too, so a less-preferred candidate winning the
        # quorum race doesn't lock in over a known-better one; under
        # equivocate we accept any cid (that's the point of R14).
        target = self._target_commit(p)
        if len(srcs) >= self._eff_quorum() and not p.synced:
            if cid in p.has_body and (self.cfg.equivocate or cid == target):
                p.synced = True
                p.applied_commit = cid
                if p.diagnosis != "got-es":
                    p.diagnosis = "type3-netfail"
                p.note(ev.time, f"SYNCED on {cid} (q={self._eff_quorum()}, have body)")

    def _bodyreq_grace(self, ev):
        # Grace elapsed: if the body still hasn't arrived, request it now.
        p = self.participants[ev.dst]
        cid = ev.payload["commit_id"]
        if cid in p.has_body or p.synced:
            return  # body arrived on its own; no request needed
        self._send_bodyreq(p, ev.time, cid)

    def _send_bodyreq(self, p, t, cid):
        # Broadcast a light body-request; schedule a retry in case the body
        # (or the request) is dropped. Give up after max_bodyreq attempts
        # FOR THIS CID -- each candidate gets its own retry budget, since
        # under all_mint a participant may need to chase several distinct
        # candidates (e.g. C-S0 and C-S2) at once.
        n = p.bodyreq_attempts.get(cid, 0)
        if n >= self.cfg.max_bodyreq:
            return
        p.bodyreq_attempts[cid] = n + 1
        self.broadcast(t, p.id, BODYREQ, {"commit_id": cid})
        p.note(t, f"BODYREQ {cid} (attempt {n + 1})")
        self._push(t + self.cfg.bodyreq_retry, "BODYREQ_RETRY", p.id, p.id,
                   {"commit_id": cid})

    def _bodyreq_retry(self, ev):
        p = self.participants[ev.dst]
        cid = ev.payload["commit_id"]
        if cid in p.has_body or p.synced:
            return  # got it meanwhile
        self._send_bodyreq(p, ev.time, cid)

    def _deliver_bodyreq(self, ev):
        # Only a small, deterministic pool of lowest-rank holders ever
        # consider answering a given request (bounds the cost regardless of
        # how many holders exist). Each eligible holder's answer is staggered
        # by rank (answer_stagger >= delay_max), and it cancels if it sees
        # another (lower-ranked) holder's answer arrive first. The answer
        # itself is a BROADCAST, not unicast: this is what lets it also
        # satisfy every OTHER currently-missing participant, not just the
        # one who happened to ask -- their own later-staggered request
        # attempts simply find they already have the body and skip.
        #
        # A cooldown (not a permanent flag) guards against re-triggering: a
        # holder can't see its OWN broadcast (self-delivery is skipped), so
        # without a cooldown, every new incoming request from a DIFFERENT
        # straggler would look "fresh" and cause another full broadcast.
        # The cooldown lets a genuine retry (seconds later) still get a
        # fresh answer if the first broadcast never reached that straggler.
        p = self.participants[ev.dst]
        cid = ev.payload["commit_id"]
        if self.cfg.equivocate and p.malicious and cid == "C-EVIL" and cid in p.has_body:
            # Malicious participants serve the forged body directly,
            # bypassing the polite rank/cooldown rules honest holders follow.
            self.broadcast(ev.time, p.id, COMMIT, {"commit_id": cid})
            p.note(ev.time, "malicious: serve forged C-EVIL body")
            return
        if (cid not in p.has_body or p.malicious or cid in p.bodyreq_pending
                or ev.time < p.answer_cooldown_until.get(cid, -1)):
            return
        if self.rank_of[p.id] >= self.cfg.answer_pool_size:
            return  # not in the eligible pool, never answers
        p.bodyreq_pending.add(cid)
        stagger = self._stagger_delay(p.id, self.cfg.answer_stagger,
                                      self.cfg.answer_pool_size)
        self._push(ev.time + stagger, "BODYREQ_ANSWER_CHECK", p.id, p.id,
                   {"commit_id": cid})

    def _bodyreq_answer_check(self, ev):
        p = self.participants[ev.dst]
        cid = ev.payload["commit_id"]
        p.bodyreq_pending.discard(cid)  # this cycle is done either way
        p.answer_cooldown_until[cid] = ev.time + self.cfg.answer_cooldown
        # Cancel if a lower-ranked holder's broadcast answer already arrived
        # (any sender other than the ES's own id=0 t=0 broadcast means it
        # was an answer, not the original commit).
        srcs = p.commit_sources.get(cid, set())
        if any(s != 0 for s in srcs):
            p.note(ev.time, f"cancel body answer for {cid} (already answered)")
            return
        self.broadcast(ev.time, p.id, COMMIT, {"commit_id": cid})
        p.note(ev.time, f"broadcast body answer for {cid} (rank {self.rank_of[p.id]})")

    def _r2_check(self, ev):
        # reflexion2 deadline. If we hold our target's body, attest it
        # (light). If not, broadcast NO-ES so holders attest and quorum can
        # form. Under equivocate, malicious participants instead forge and
        # attest a fake "C-EVIL" commit rather than staying silent.
        p = self.participants[ev.dst]
        if self.cfg.equivocate and p.malicious and self._can_attest(p):
            p.has_body.add("C-EVIL")
            self.broadcast(ev.time, p.id, ATTEST, {"commit_id": "C-EVIL"})
            p.attested = True
            p.note(ev.time, "malicious: equivocate, attest forged C-EVIL")
            return
        target = self._target_commit(p) if self.cfg.all_mint else "C-ES"
        if target is not None and target in p.has_body:
            self._maybe_attest(p, ev.time, target)
            return
        # no body for our target: announce NO-ES (light). BS emitting its own
        # alternative body is only meaningful in the single-ES flow --
        # all_mint already has every steward minting at t=0.
        p.note(ev.time, f"no {target or 'C-ES'} body by deadline -> NO-ES")
        self.broadcast(ev.time, p.id, NOES, {})
        if (not self.cfg.all_mint and p.role == "BS" and not p.sent_own_commit
                and "C-ES" not in p.has_body):
            p.sent_own_commit = True
            self.broadcast(ev.time, p.id, COMMIT, {"commit_id": "C-BS"})
            if not self.cfg.apply_only_on_quorum:
                p.applied_commit = "C-BS"
            p.note(ev.time, "BS emits own body C-BS")

    def _deliver_noes_r2(self, p, t):
        # In reflexion2, a holder answers NO-ES by attesting (light), not by
        # resending the heavy body. Body travels only on explicit BODYREQ.
        if "C-ES" in p.has_body:
            self._maybe_attest(p, t, "C-ES")

    # ---- run ------------------------------------------------------------
    def run(self):
        cfg = self.cfg
        es = self.participants[0]
        bs = self.participants[1] if cfg.n_stewards >= 2 else None

        if cfg.all_mint:
            # RFC's actual "multiple stewards MAY issue commit messages
            # within the same epoch": every steward mints its OWN candidate
            # at t=0, independently, none waiting for the others.
            for sid in range(cfg.n_stewards):
                cid = f"C-S{sid}"
                st = self.participants[sid]
                st.commit_sources.setdefault(cid, set()).add(sid)
                st.has_body.add(cid)
                self.broadcast(0.0, sid, COMMIT, {"commit_id": cid})
                if not cfg.apply_only_on_quorum:
                    st.applied_commit = self._held_commit(st)
                    st.diagnosis = "got-es"
                st.note(0.0, f"steward {sid} mints {cid}")
            es_afk = False
        else:
            # ES may be afk: model as ES simply not committing.
            # We flip a coin using malicious? No: afk is a reliability event,
            # model it as ES committing normally here; drops create the
            # "afk-looking" case. ES broadcasts its commit at t=0.
            es_afk = getattr(cfg, "_es_afk", False)
            if not es_afk:
                self.broadcast(0.0, es.id, COMMIT, {"commit_id": "C-ES"})
                if not cfg.apply_only_on_quorum:
                    es.applied_commit = "C-ES"
                    es.diagnosis = "got-es"
                es.has_body.add("C-ES")
                es.commit_sources.setdefault("C-ES", set()).add(es.id)
                es.synced = True
                es.note(0.0, "ES broadcast C-ES")
            else:
                es.note(0.0, "ES is AFK, no commit")

        # Scheduling of the "no ES commit by deadline" reaction.
        if cfg.scenario == "reflexion":
            # everyone (except ES) may announce NO-ES and answer
            for pid, p in self.participants.items():
                if pid == es.id:
                    continue
                self._push(cfg.noes_wait, "NOES_CHECK", pid, pid, {})
        elif cfg.scenario in ("reflexion2", "committee", "hybrid"):
            # everyone (all_mint: everyone including stewards; otherwise
            # everyone except ES) checks at deadline: if they hold their
            # target's body they attest (light, committee-gated); if not,
            # they send NO-ES / a body request so they can still sync.
            for pid, p in self.participants.items():
                if not cfg.all_mint and pid == es.id:
                    continue
                self._push(cfg.noes_wait, "R2_CHECK", pid, pid, {})
            if cfg.scenario == "hybrid":
                # SDS-style proactive push on top of committee attestation:
                # the ES keeps re-pushing the body until everyone has it,
                # instead of waiting for a reactive bodyreq round trip.
                self._push(cfg.sds_rebroadcast, "HYBRID_PUSH", es.id, es.id, {})
        elif cfg.scenario == "plain" and bs is not None and not cfg.all_mint:
            # plain has no sync, but BS still emits its OWN commit if it did
            # not receive the ES commit -> this is what creates real forks.
            # (Under all_mint, every steward -- including BS -- already
            # minted its own candidate above; no separate fallback needed.)
            self._push(cfg.noes_wait, "BS_DEADLINE", bs.id, bs.id, {})
        elif cfg.scenario == "sds":
            es.seen_ids.add("C-ES")
            self._push(cfg.sds_rebroadcast, "SDS_REBROADCAST", es.id, es.id, {})
        elif cfg.scenario == "heartbeat":
            # ES starts heartbeating the moment it commits; BS must not decide
            # until the full heartbeat window has had a chance to reach it,
            # otherwise it judges "afk" on far fewer than hb_window/hb_interval
            # chances, undermining the whole point of the heartbeat redundancy.
            self._push(cfg.hb_interval, "HB_CHECK", es.id, es.id, {})
            if bs is not None:
                self._push(cfg.hb_window + cfg.hb_interval, "BS_DEADLINE",
                           bs.id, bs.id, {})
        elif cfg.scenario == "hbclose":
            # Phase 1: ES heartbeats/pushes C-ES every hb_interval. Every
            # OTHER participant makes its one classification decision
            # exactly once, at t=hb_window (have body / conclude ES afk,
            # possibly triggering BS's phase 2 / raise a staggered NO-ES).
            self._push(cfg.hb_interval, "HB_CHECK", es.id, es.id, {})
            for pid, p in self.participants.items():
                if pid == es.id:
                    continue
                self._push(cfg.hb_window, "HBCLOSE_CHECK", pid, pid,
                           {"commit_id": "C-ES"})

        while self.q:
            ev = heapq.heappop(self.q)
            if ev.time > cfg.max_time:
                break
            if ev.kind == COMMIT:
                self._deliver_commit(ev)
            elif ev.kind == NOES:
                self._deliver_noes(ev)
            elif ev.kind == ATTEST:
                self._deliver_attest(ev)
            elif ev.kind == BODYREQ:
                self._deliver_bodyreq(ev)
            elif ev.kind == "BODYREQ_ANSWER_CHECK":
                self._bodyreq_answer_check(ev)
            elif ev.kind == ACK:
                self._deliver_ack(ev)
            elif ev.kind == "SDS_REBROADCAST":
                self._sds_rebroadcast_check(ev)
            elif ev.kind == "HYBRID_PUSH":
                self._hybrid_push_check(ev)
            elif ev.kind == HEARTBEAT:
                self._deliver_heartbeat(ev)
            elif ev.kind == "HB_CHECK":
                self._es_heartbeat_check(ev)
            elif ev.kind == "HB_CHECK_BS":
                self._bs_heartbeat_check(ev)
            elif ev.kind == "HBCLOSE_CHECK":
                self._hbclose_check(ev)
            elif ev.kind == "HBCLOSE_NOES":
                self._hbclose_noes(ev)
            elif ev.kind == "NOES_CHECK":
                self._noes_check(ev)
            elif ev.kind == "R2_CHECK":
                self._r2_check(ev)
            elif ev.kind == "ATTEST_CHECK":
                self._attest_check(ev)
            elif ev.kind == "BODYREQ_RETRY":
                self._bodyreq_retry(ev)
            elif ev.kind == "BODYREQ_GRACE":
                self._bodyreq_grace(ev)
            elif ev.kind == "BS_DEADLINE":
                self._bs_deadline(ev)

        # Final diagnosis pass: a member that never got any target commit and
        # never reached quorum concludes the (relevant) steward was afk.
        if cfg.scenario in ("reflexion", "reflexion2", "committee", "hybrid", "heartbeat"):
            for p in self.participants.values():
                if p.diagnosis is None and not p.synced:
                    saw_any = (any(c.startswith("C-S") for c in p.commit_sources)
                              if cfg.all_mint else "C-ES" in p.commit_sources)
                    if not saw_any:
                        p.diagnosis = "type2-afk"
                        p.note(cfg.max_time, "no commit, no quorum -> afk (type2)")

        return self._result(es_afk)

    # ---- result / classification ---------------------------------------
    def _result(self, es_afk):
        members = list(self.participants.values())
        applied = [p.applied_commit for p in members]

        # success = every participant on the exact same non-None commit
        distinct = set(applied)
        stalled = any(a is None for a in applied)
        forked = len([d for d in distinct if d is not None]) > 1
        success = (not stalled) and (not forked) and (None not in distinct)

        if success:
            status = "SUCCESS"
        elif forked:
            status = "FAIL-FORK"
        elif stalled:
            status = "FAIL-STALL"
        else:
            status = "FAIL"

        # diagnosis tally: how each member explained its situation
        got_es = sum(1 for p in members if p.diagnosis == "got-es")
        type2 = sum(1 for p in members if p.diagnosis == "type2-afk")
        type3 = sum(1 for p in members if p.diagnosis == "type3-netfail")
        undiag = sum(1 for p in members if p.diagnosis is None)

        return {
            "status": status,
            "success": success,
            "applied": applied,
            "distinct": distinct,
            "msg_count": self.msg_count,
            "byte_count": self.byte_count,
            "quorum": self.quorum,
            "n": self.n,
            "es_afk": es_afk,
            "got_es": got_es,
            "type2": type2,
            "type3": type3,
            "undiag": undiag,
        }


# --------------------------------------------------------------------------
# Trial loop
# --------------------------------------------------------------------------
def run_trials(cfg: Config, es_afk_prob: float = 0.0):
    successes = 0
    forks = 0
    stalls = 0
    total_msgs = 0
    total_bytes = 0
    tot_got_es = tot_type2 = tot_type3 = 0
    for i in range(cfg.n_trials):
        rng = random.Random(cfg.seed + i)
        # optionally make ES afk in some fraction of trials
        local = Config(**vars(cfg))
        local._es_afk = rng.random() < es_afk_prob
        sim = Simulator(local, rng)
        res = sim.run()
        total_msgs += res["msg_count"]
        total_bytes += res["byte_count"]
        tot_got_es += res["got_es"]
        tot_type2 += res["type2"]
        tot_type3 += res["type3"]
        if res["success"]:
            successes += 1
        elif res["status"] == "FAIL-FORK":
            forks += 1
        elif res["status"] == "FAIL-STALL":
            stalls += 1
    t = cfg.n_trials
    return {
        "trials": t,
        "successes": successes,
        "forks": forks,
        "stalls": stalls,
        "success_rate": successes / t,
        "avg_msgs": total_msgs / t,
        "avg_bytes": total_bytes / t,
        # average members per trial in each diagnosis bucket
        "avg_got_es": tot_got_es / t,
        "avg_type2": tot_type2 / t,
        "avg_type3": tot_type3 / t,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_argparser():
    ap = argparse.ArgumentParser(description="de-MLS reliability simulator")
    ap.add_argument("--plain", action="store_const", dest="scenario",
                    const="plain", help="plain scenario (no sync)")
    ap.add_argument("--reflexion", action="store_const", dest="scenario",
                    const="reflexion", help="reflexion sync scenario")
    ap.add_argument("--reflexion2", action="store_const", dest="scenario",
                    const="reflexion2",
                    help="optimized reflexion: light attest + body pull")
    ap.add_argument("--committee", action="store_const", dest="scenario",
                    const="committee",
                    help="control-committee scenario (large-group optimization)")
    ap.add_argument("--sds", action="store_const", dest="scenario",
                    const="sds",
                    help="SDS-style periodic rebroadcast + ack (vac/raw/sds.md)")
    ap.add_argument("--hybrid", action="store_const", dest="scenario",
                    const="hybrid",
                    help="committee decision + SDS-style proactive body push")
    ap.add_argument("--heartbeat", action="store_const", dest="scenario",
                    const="heartbeat",
                    help="ES self-announces existence repeatedly; no fork to BS "
                         "once any heartbeat confirms the commit exists")
    ap.add_argument("--hbclose", action="store_const", dest="scenario",
                    const="hbclose",
                    help="single steward only; heartbeat is a pure existence "
                         "signal, all decisions made once at window close")
    ap.add_argument("--stewards", type=int)
    ap.add_argument("--members", type=int)
    ap.add_argument("--p-loss", type=float)
    ap.add_argument("--malicious", type=float)
    ap.add_argument("--trials", type=int)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--apply-only-on-quorum", action="store_true", default=None,
                    help="never apply before body+quorum (0 forks, stalls instead)")
    ap.add_argument("--equivocate", action="store_true", default=None,
                    help="malicious participants forge and serve a fake commit")
    ap.add_argument("--all-mint", action="store_true", default=None,
                    help="every steward mints its own commit at t=0 (RFC's "
                         "actual concurrent-committer behavior)")
    ap.add_argument("--es-afk-prob", type=float, default=0.0,
                    help="fraction of trials where ES is genuinely afk")
    return ap


def cfg_from_args(args):
    cfg = Config()
    if args.scenario: cfg.scenario = args.scenario
    if args.stewards is not None: cfg.n_stewards = args.stewards
    if args.members is not None: cfg.n_members = args.members
    if args.p_loss is not None: cfg.p_loss = args.p_loss
    if args.malicious is not None: cfg.malicious_ratio = args.malicious
    if args.trials is not None: cfg.n_trials = args.trials
    if args.seed is not None: cfg.seed = args.seed
    if args.apply_only_on_quorum: cfg.apply_only_on_quorum = True
    if args.equivocate: cfg.equivocate = True
    if args.all_mint: cfg.all_mint = True
    return cfg


if __name__ == "__main__":
    args = build_argparser().parse_args()
    cfg = cfg_from_args(args)
    out = run_trials(cfg, es_afk_prob=args.es_afk_prob)
    print(f"scenario={cfg.scenario} n={cfg.n_stewards + cfg.n_members} "
          f"p_loss={cfg.p_loss} malicious={cfg.malicious_ratio}")
    print(f"trials={out['trials']} success={out['successes']} "
          f"fork={out['forks']} stall={out['stalls']}")
    print(f"success_rate={out['success_rate']:.3f} avg_msgs={out['avg_msgs']:.1f}")
