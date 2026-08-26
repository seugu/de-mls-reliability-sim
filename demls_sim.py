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
    apply_only_on_quorum: bool = False # never settle on a commit before its
                                 # 2n/3 quorum forms; under <n/3 Byzantine the
                                 # quorum is unique, so a short member stalls
                                 # instead of forking
    equivocate: bool = False     # malicious attesters equivocate a fabricated
                                 # commit (C-EVIL) instead of staying silent
    all_mint: bool = False       # RFC-faithful concurrent minting: every steward
                                 # mints its own distinct commit at once (no
                                 # primary-first ES/BS), members select over what
                                 # they received. Exercised on the plain baseline.
    n_trials: int = 500          # random trials per run
    seed: int = 0                # base RNG seed (trial i uses seed+i)
    scenario: str = "plain"      # plain|reflexion|reflexion2|committee


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
        self.body_requested = False   # did we already send a body request?
        self.bodyreq_attempts = 0     # how many body requests we sent
        # all-mint: the winner can shift as better candidates are learned, so we
        # track which commit we last attested / requested rather than a bare flag
        self.attested_cid = None      # commit id of our last attestation
        self.body_req_cid = None      # commit id we last requested the body of
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
        self.last_change_time = 0.0   # sim time the group last changed a settled
                                      # commit; the t=0 mints count, so start at 0
        self.participants = {}
        self.n = cfg.n_stewards + cfg.n_members
        self.quorum = quorum_size(cfg, self.n)
        self.epoch = 5                # single-epoch sim; fixed value for hashing
        self.committee = self._select_committee()
        # committee quorum = ceil(2/3 of committee size)
        self.committee_quorum = -(-(2 * len(self.committee)) // 3) if self.committee else 0
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
        else:  # reflexion2 or committee
            self._reflexion2_on_commit(p, ev)

    def _deliver_noes(self, ev):
        p = self.participants[ev.dst]
        p.seen_noes.add(ev.src)
        p.note(ev.time, f"recv NO-ES from {ev.src}")
        if self.cfg.scenario == "reflexion2" or (
                self.cfg.all_mint and self.cfg.scenario == "committee"):
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
        """Best commit a participant holds. all-mint (RFC concurrent minting):
        the epoch steward's commit if held, else the smallest committer id among
        those held -- the RFC selection collapses to this when every steward
        commits the same proposals. Otherwise (primary-first): ES, then BS, else
        any."""
        if self.cfg.all_mint:
            # Steward 0 is the epoch steward; its commit is "C-S0".
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
        # BS cannot tell afk from network drop, just like any member.
        p = self.participants[ev.dst]
        if p.role != "BS":
            return
        if "C-ES" in p.commit_sources:
            return  # saw ES commit, no need to emit own
        if p.sent_own_commit:
            return
        p.sent_own_commit = True
        self.broadcast(ev.time, p.id, COMMIT, {"commit_id": "C-BS"})
        p.applied_commit = "C-BS"
        p.note(ev.time, "BS deadline: no ES commit -> emit + apply C-BS")

    # ---- reflexion2 / committee shared helpers -------------------------
    def _eff_quorum(self):
        # committee scenario uses the smaller committee-internal 2/3 threshold;
        # reflexion2 uses the global 2n/3.
        if self.cfg.scenario == "committee":
            return self.committee_quorum
        return self.quorum

    def _can_attest(self, p):
        # In committee mode only committee members attest; otherwise anyone with
        # the body may attest.
        if self.cfg.scenario == "committee":
            return p.id in self.committee
        return True

    def _target_commit(self, p):
        # all-mint: the deterministic winner among the candidates a member has
        # heard of -- by body OR by attestation. Existence, not possession, sets
        # the target, so learning a better candidate's attestation is enough to
        # aim at it and then pull its body. Epoch steward (C-S0) is the smallest.
        known = [c for c in set(p.commit_sources) | set(p.attest_sources)
                 if c.startswith("C-S")]
        if not known:
            return None
        return min(known, key=lambda c: int(c[3:]))

    def _r2_progress(self, p, t):
        # all-mint sync driver: aim a member at the current winner, and re-aim it
        # as better candidates are learned. It attests the winner (spreading the
        # winner's existence even when its body did not travel), pulls the
        # winner's body if missing, and syncs once it holds the body and sees a
        # quorum attesting it.
        target = self._target_commit(p)
        if target is None:
            return
        srcs = p.attest_sources.get(target, set())
        if target in p.has_body and len(srcs) >= self._eff_quorum() and not p.synced:
            p.synced = True
            p.applied_commit = target
            if p.diagnosis != "got-es":
                p.diagnosis = "type3-netfail"
            p.note(t, f"SYNCED on {target} (body + quorum)")
            return
        # Provisional apply of the winner we hold, unless quorum-gated. A member
        # that already applied a worse candidate upgrades once it holds the
        # winner's body; without the gate it never forks for good.
        if (target in p.has_body and p.applied_commit != target
                and not self.cfg.apply_only_on_quorum):
            p.applied_commit = target
            if target in p.commit_sources:
                p.diagnosis = "got-es"
        self._maybe_attest(p, t, target)
        if target not in p.has_body and p.body_req_cid != target and not p.synced:
            p.body_req_cid = target
            p.bodyreq_attempts = 0
            p.diagnosis = "type3-netfail"
            self._push(t + self.cfg.bodyreq_grace, "BODYREQ_GRACE", p.id, p.id,
                       {"commit_id": target})

    # ---- reflexion2 scenario (attestation + body pull) -----------------
    def _reflexion2_on_commit(self, p, ev):
        if self.cfg.all_mint:
            self._r2_progress(p, ev.time)
            return
        # Got the ES body directly -> apply and attest.
        cid = ev.payload["commit_id"]
        if cid == "C-ES" and p.applied_commit is None:
            p.diagnosis = "got-es"
            # Normally a member applies the ES commit the moment it holds the
            # body. Under apply_only_on_quorum it waits for the 2n/3 quorum, so a
            # member short of a candidate stalls rather than applying a commit
            # the group may not settle on.
            if not self.cfg.apply_only_on_quorum:
                p.applied_commit = "C-ES"
                p.note(ev.time, "have ES body -> apply C-ES")
        # If we now hold the body and already have quorum attestations, sync.
        srcs = p.attest_sources.get(cid, set())
        if cid in p.has_body and len(srcs) >= self._eff_quorum() and not p.synced:
            p.synced = True
            p.applied_commit = cid
            if p.diagnosis != "got-es":
                p.diagnosis = "type3-netfail"
            p.note(ev.time, f"SYNCED on {cid} (body arrived, quorum held)")
        # We hold a body, so we can attest it (light message).
        self._maybe_attest(p, ev.time, cid)

    def _maybe_attest(self, p, t, cid):
        # Send a light ATTEST after a short suppression wait. Primary-first
        # attests once; all-mint may re-attest when the winner shifts to a
        # better candidate (targets only decrease, so this is bounded).
        if p.malicious or not self._can_attest(p):
            return
        if self.cfg.all_mint:
            if p.attested_cid == cid:
                return
        elif p.attested:
            return
        self._push(t + self.cfg.suppress_wait, "ATTEST_CHECK", p.id, p.id,
                   {"commit_id": cid})

    def _attest_check(self, ev):
        p = self.participants[ev.dst]
        cid = ev.payload["commit_id"]
        if self.cfg.all_mint:
            # Attest the current winner, which may have moved since this check
            # was scheduled.
            target = self._target_commit(p)
            if target is None or p.attested_cid == target:
                return
            p.attested_cid = target
            srcs = p.attest_sources.get(target, set())
            if len(srcs) >= self._eff_quorum():
                p.note(ev.time, f"suppressed ATTEST {target} (quorum already seen)")
                return
            self.broadcast(ev.time, p.id, ATTEST, {"commit_id": target})
            p.note(ev.time, f"broadcast ATTEST {target}")
            return
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

        if self.cfg.all_mint:
            self._r2_progress(p, ev.time)
            return

        # If a commit is being attested but we lack its body, we will pull it,
        # but not immediately: the body may still be in flight. Schedule a grace
        # check; only request if the body still hasn't arrived by then.
        if cid not in p.has_body and not p.body_requested and not p.synced:
            p.body_requested = True  # mark intent so we schedule only once
            p.diagnosis = "type3-netfail"
            self._push(ev.time + self.cfg.bodyreq_grace, "BODYREQ_GRACE",
                       p.id, p.id, {"commit_id": cid})

        # Quorum of distinct attestations => the commit provably exists.
        # Sync completes only once we ALSO hold the body.
        if len(srcs) >= self._eff_quorum() and not p.synced:
            if cid in p.has_body:
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
        # (or the request) is dropped. Give up after max_bodyreq attempts.
        if p.bodyreq_attempts >= self.cfg.max_bodyreq:
            return
        p.bodyreq_attempts += 1
        self.broadcast(t, p.id, BODYREQ, {"commit_id": cid})
        p.note(t, f"BODYREQ {cid} (attempt {p.bodyreq_attempts})")
        self._push(t + self.cfg.bodyreq_retry, "BODYREQ_RETRY", p.id, p.id,
                   {"commit_id": cid})

    def _bodyreq_retry(self, ev):
        p = self.participants[ev.dst]
        cid = ev.payload["commit_id"]
        if cid in p.has_body or p.synced:
            return  # got it meanwhile
        self._send_bodyreq(p, ev.time, cid)

    def _deliver_bodyreq(self, ev):
        # Any holder of the requested body answers the REQUESTER directly
        # (unicast), so the heavy body does not hit the whole channel.
        # Unicast keeps this cheap even if several holders answer.
        p = self.participants[ev.dst]
        cid = ev.payload["commit_id"]
        if p.malicious:
            # A Byzantine holder serves a fabricated body to anyone who asks for
            # the equivocated commit. Only a member already fooled by a quorum of
            # equivocations asks, which the <n/3 assumption rules out.
            if self.cfg.equivocate and cid == "C-EVIL":
                self.unicast(ev.time, p.id, ev.src, COMMIT, {"commit_id": cid})
            return
        if cid not in p.has_body:
            return
        self.unicast(ev.time, p.id, ev.src, COMMIT, {"commit_id": cid})
        p.note(ev.time, f"answer BODYREQ from {ev.src}: unicast body {cid}")

    def _r2_check(self, ev):
        # reflexion2 deadline. If we hold the ES body, attest it (light).
        # If not, broadcast NO-ES so holders attest and quorum can form.
        p = self.participants[ev.dst]
        if p.malicious and self.cfg.equivocate and self._can_attest(p):
            # <n/3 assumption: a Byzantine attester may equivocate, attesting a
            # commit it does not hold. It cannot reach a 2n/3 quorum without
            # honest attesters, so the forgery only lands past the threshold.
            p.attested = True
            self.broadcast(ev.time, p.id, ATTEST, {"commit_id": "C-EVIL"})
            p.note(ev.time, "malicious: equivocate ATTEST C-EVIL")
            return
        if self.cfg.all_mint:
            # all-mint: aim at the winner among known candidates; if none reached
            # us, announce NO-ES so holders attest and we learn one.
            if self._target_commit(p) is None:
                p.note(ev.time, "no candidate by deadline -> NO-ES")
                self.broadcast(ev.time, p.id, NOES, {})
            else:
                self._r2_progress(p, ev.time)
            return
        if "C-ES" in p.has_body:
            self._maybe_attest(p, ev.time, "C-ES")
            return
        # no ES body: announce NO-ES (light). BS also emits its own body.
        p.note(ev.time, "no ES body by deadline -> NO-ES")
        self.broadcast(ev.time, p.id, NOES, {})
        if p.role == "BS" and not p.sent_own_commit and "C-ES" not in p.has_body:
            p.sent_own_commit = True
            self.broadcast(ev.time, p.id, COMMIT, {"commit_id": "C-BS"})
            # The backup applies its own commit only when we let it settle before
            # a quorum. Held to the quorum, it emits C-BS but waits, so it cannot
            # strand itself on a commit the group settles against.
            if not self.cfg.apply_only_on_quorum:
                p.applied_commit = "C-BS"
            p.note(ev.time, "BS emits own body C-BS")

    def _deliver_noes_r2(self, p, t):
        # In reflexion2, a holder answers NO-ES by attesting (light), not by
        # resending the heavy body. Body travels only on explicit BODYREQ.
        if self.cfg.all_mint:
            self._r2_progress(p, t)
            return
        if "C-ES" in p.has_body:
            self._maybe_attest(p, t, "C-ES")

    # ---- run ------------------------------------------------------------
    def run(self):
        cfg = self.cfg
        es = self.participants[0]
        bs = self.participants[1] if cfg.n_stewards >= 2 else None

        # ES may be afk: model as ES simply not committing.
        # We flip a coin using malicious? No: afk is a reliability event, model
        # it as ES committing normally here; drops create the "afk-looking" case.
        # ES broadcasts its commit at t=0.
        es_afk = getattr(cfg, "_es_afk", False)
        if cfg.all_mint:
            # RFC concurrent minting: every steward mints its own commit at the
            # same time. Each carries its own committer entropy, so the commits
            # are distinct (distinct keys). A member settles on the best of the
            # ones it receives; the ones it misses are what split the group.
            for sid in range(cfg.n_stewards):
                cid = f"C-S{sid}"
                st = self.participants[sid]
                st.commit_sources.setdefault(cid, set()).add(sid)
                st.has_body.add(cid)
                self.broadcast(0.0, sid, COMMIT, {"commit_id": cid})
                if not cfg.apply_only_on_quorum:
                    st.applied_commit = self._held_commit(st)
                st.note(0.0, f"steward {sid} mints {cid}")
        elif not es_afk:
            self.broadcast(0.0, es.id, COMMIT, {"commit_id": "C-ES"})
            es.has_body.add("C-ES")
            es.diagnosis = "got-es"
            # The committer settles on its own commit at once, unless we hold
            # every member -- the ES included -- to the 2n/3 quorum. Then the ES
            # waits too, so a lost round leaves it stalled with the rest instead
            # of forked onto a commit no one else ever confirmed.
            if not cfg.apply_only_on_quorum:
                es.applied_commit = "C-ES"
                es.synced = True
            es.note(0.0, "ES broadcast C-ES")
        else:
            es.note(0.0, "ES is AFK, no commit")

        # Scheduling of the "no ES commit by deadline" reaction.
        if cfg.all_mint:
            # With a sync layer every participant drives toward the winner at its
            # deadline; plain all-mint has no reaction (the RFC baseline).
            if cfg.scenario in ("reflexion2", "committee"):
                for pid in self.participants:
                    self._push(cfg.noes_wait, "R2_CHECK", pid, pid, {})
        elif cfg.scenario == "reflexion":
            # everyone (except ES) may announce NO-ES and answer
            for pid, p in self.participants.items():
                if pid == es.id:
                    continue
                self._push(cfg.noes_wait, "NOES_CHECK", pid, pid, {})
        elif cfg.scenario in ("reflexion2", "committee"):
            # everyone (except ES) checks at deadline: if they hold the ES body
            # they attest (light, committee-only in committee mode); if not, they
            # send NO-ES / a body request so they can still sync.
            for pid, p in self.participants.items():
                if pid == es.id:
                    continue
                self._push(cfg.noes_wait, "R2_CHECK", pid, pid, {})
        elif cfg.scenario == "plain" and bs is not None:
            # plain has no sync, but BS still emits its OWN commit if it did
            # not receive the ES commit -> this is what creates real forks.
            self._push(cfg.noes_wait, "BS_DEADLINE", bs.id, bs.id, {})

        while self.q:
            ev = heapq.heappop(self.q)
            if ev.time > cfg.max_time:
                break
            # Snapshot settled commits so we can time convergence: the last event
            # that moved any member's applied commit is when the group finished.
            before = [p.applied_commit for p in self.participants.values()]
            if ev.kind == COMMIT:
                self._deliver_commit(ev)
            elif ev.kind == NOES:
                self._deliver_noes(ev)
            elif ev.kind == ATTEST:
                self._deliver_attest(ev)
            elif ev.kind == BODYREQ:
                self._deliver_bodyreq(ev)
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
            if [p.applied_commit for p in self.participants.values()] != before:
                self.last_change_time = ev.time

        # Final diagnosis pass: a member that never got the commit and never
        # reached quorum concludes the ES was afk (type 2).
        if cfg.scenario in ("reflexion", "reflexion2"):
            for p in self.participants.values():
                if p.diagnosis is None and not p.synced:
                    if "C-ES" not in p.commit_sources:
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
            "convergence_time": self.last_change_time,
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
    conv_times = []               # convergence time, successful trials only
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
            conv_times.append(res["convergence_time"])
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
        # mean sim time to converge, over successful trials (None if none)
        "avg_convergence": (sum(conv_times) / len(conv_times)
                            if conv_times else None),
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
    ap.add_argument("--apply-only-on-quorum", action="store_true",
                    help="never settle on a commit before its 2n/3 quorum")
    ap.add_argument("--equivocate", action="store_true",
                    help="malicious attesters equivocate a fabricated commit")
    ap.add_argument("--all-mint", action="store_true",
                    help="RFC concurrent minting: every steward mints at once")
    ap.add_argument("--stewards", type=int)
    ap.add_argument("--members", type=int)
    ap.add_argument("--p-loss", type=float)
    ap.add_argument("--malicious", type=float)
    ap.add_argument("--trials", type=int)
    ap.add_argument("--seed", type=int)
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
