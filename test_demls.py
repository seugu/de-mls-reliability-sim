#!/usr/bin/env python3
"""
Self-checking requirement loop for the de-MLS reliability simulator.

Each check encodes one requirement we agreed on. The loop runs them all and
only reports PASS if every requirement holds. If any fails, it prints which
one and exits non-zero.
"""

import sys
from demls_sim import Config, Simulator, run_trials, quorum_size
import random


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail else ""))
    return cond


def main():
    results = []

    # R1: quorum is ceil(2n/3)
    c = Config(n_stewards=3, n_members=9)
    q = quorum_size(c, 12)
    results.append(check("R1 quorum = ceil(2n/3)", q == 8, f"got {q}, expected 8"))

    # R2: with zero loss, plain reaches full agreement (no fork)
    c = Config(scenario="plain", p_loss=0.0, n_stewards=3, n_members=9,
               n_trials=100, seed=1)
    out = run_trials(c)
    results.append(check("R2 plain, p_loss=0 -> always success",
                         out["success_rate"] == 1.0,
                         f"rate={out['success_rate']:.3f}"))

    # R3: plain with loss can fork (fork rate > 0)
    c = Config(scenario="plain", p_loss=0.2, n_stewards=3, n_members=9,
               n_trials=300, seed=2)
    out = run_trials(c)
    results.append(check("R3 plain, p_loss>0 -> forks appear",
                         out["forks"] > 0,
                         f"forks={out['forks']}/{out['trials']}"))

    # R4: reflexion with zero loss -> always success
    c = Config(scenario="reflexion", p_loss=0.0, n_stewards=3, n_members=9,
               n_trials=100, seed=3)
    out = run_trials(c)
    results.append(check("R4 reflexion, p_loss=0 -> always success",
                         out["success_rate"] == 1.0,
                         f"rate={out['success_rate']:.3f}"))

    # R5: reflexion should beat plain at the same loss (higher success rate)
    common = dict(p_loss=0.15, n_stewards=3, n_members=9, n_trials=400, seed=4)
    p_out = run_trials(Config(scenario="plain", **common))
    r_out = run_trials(Config(scenario="reflexion", **common))
    results.append(check("R5 reflexion success >= plain success",
                         r_out["success_rate"] >= p_out["success_rate"],
                         f"plain={p_out['success_rate']:.3f} "
                         f"reflexion={r_out['success_rate']:.3f}"))

    # R6: message count includes drops (reflexion sends more than plain)
    results.append(check("R6 reflexion uses more msgs than plain",
                         r_out["avg_msgs"] > p_out["avg_msgs"],
                         f"plain={p_out['avg_msgs']:.1f} "
                         f"reflexion={r_out['avg_msgs']:.1f}"))

    # R7: every participant has a non-empty log we can inspect
    rng = random.Random(7)
    sim = Simulator(Config(scenario="reflexion", p_loss=0.2), rng)
    sim.run()
    all_logged = all(len(p.log) > 0 for p in sim.participants.values())
    results.append(check("R7 per-participant log populated", all_logged))

    # R8: success is defined as ALL on same commit (a hand-built fork fails)
    rng = random.Random(8)
    sim = Simulator(Config(scenario="plain", p_loss=0.0), rng)
    res = sim.run()
    # force a fork and re-evaluate the criterion
    victim = sim.participants[sim.n - 1]
    victim.applied_commit = "C-OTHER"
    res2 = sim._result(False)
    results.append(check("R8 one divergent member -> FAIL-FORK",
                         res2["status"] == "FAIL-FORK",
                         f"status={res2['status']}"))

    # R9: a stalled member (applied None) -> FAIL-STALL
    rng = random.Random(9)
    sim = Simulator(Config(scenario="plain", p_loss=0.0), rng)
    sim.run()
    victim = sim.participants[sim.n - 1]
    victim.applied_commit = None
    res3 = sim._result(False)
    results.append(check("R9 one member with no commit -> FAIL-STALL",
                         res3["status"] == "FAIL-STALL",
                         f"status={res3['status']}"))

    # R10: in the breakdown region, higher loss -> strictly lower success.
    # (below ~0.5 reflexion is at a 1.0 plateau, so compare 0.6 vs 0.85)
    lo = run_trials(Config(scenario="reflexion", p_loss=0.60,
                           n_trials=400, seed=10))
    hi = run_trials(Config(scenario="reflexion", p_loss=0.85,
                           n_trials=400, seed=10))
    results.append(check("R10 higher loss -> strictly lower success",
                         hi["success_rate"] < lo["success_rate"],
                         f"p=.60 -> {lo['success_rate']:.3f}, "
                         f"p=.85 -> {hi['success_rate']:.3f}"))

    # R11: malicious lies degrade reflexion success vs honest baseline
    base = run_trials(Config(scenario="reflexion", p_loss=0.3,
                             malicious_ratio=0.0, n_trials=400, seed=11))
    mal = run_trials(Config(scenario="reflexion", p_loss=0.3,
                            malicious_ratio=0.3, n_trials=400, seed=11))
    results.append(check("R11 malicious <= honest success",
                         mal["success_rate"] <= base["success_rate"],
                         f"honest={base['success_rate']:.3f} "
                         f"malicious={mal['success_rate']:.3f}"))

    # R12: apply-only-on-quorum removes the residual small-group fork. Under the
    # <n/3 honest assumption a 2n/3 quorum is unique, so a member that waits for
    # it can only stall, never fork. Same reflexion2 run, flag off vs on.
    common = dict(scenario="reflexion2", p_loss=0.3, n_stewards=3, n_members=9,
                  n_trials=400, seed=12)
    off = run_trials(Config(**common))
    on = run_trials(Config(apply_only_on_quorum=True, **common))
    results.append(check("R12 apply-only-on-quorum: forks -> 0",
                         off["forks"] > 0 and on["forks"] == 0,
                         f"off forks={off['forks']}, on forks={on['forks']} "
                         f"(on stalls={on['stalls']})"))

    # R13: a small committee does not inherit the global honesty bound. With the
    # membership below 1/3 Byzantine, a large committee stays under 1/3 by
    # concentration, but a steward-sized one often exceeds it -- so the
    # attestation set cannot be a handful of stewards.
    def committee_byzantine_prob(n, ratio, mal, trials=2000):
        bad = 0
        for i in range(trials):
            rng = random.Random(1000 + i)
            sim = Simulator(Config(n_stewards=1, n_members=n - 1,
                                   committee_ratio=ratio, malicious_ratio=mal), rng)
            comm = sim.committee
            liars = {pid for pid, p in sim.participants.items() if p.malicious}
            if comm and len(comm & liars) / len(comm) >= 1 / 3:
                bad += 1
        return bad / trials
    small = committee_byzantine_prob(12, 0.2, 0.25)     # ~2-member committee
    large = committee_byzantine_prob(1000, 0.2, 0.25)   # ~200-member committee
    results.append(check("R13 small committee unsafe, large committee safe",
                         small > 0.25 and large < 0.05,
                         f"P(committee>=1/3 Byz): small(n=12)={small:.3f} "
                         f"large(n=1000)={large:.4f}"))

    # R14: <n/3 Byzantine cannot forge a commit. Equivocators attest a
    # fabricated C-EVIL and serve a fake body, but applying it needs a 2n/3
    # quorum only honest attesters could supply. At a third malicious no honest
    # member applies it; only past the threshold does the forgery land.
    def evil_applied(mal, seed):
        total = 0
        for i in range(200):
            rng = random.Random(seed + i)
            sim = Simulator(Config(scenario="reflexion2", equivocate=True,
                                   apply_only_on_quorum=True, p_loss=0.05,
                                   n_stewards=3, n_members=9,
                                   malicious_ratio=mal), rng)
            sim.run()
            total += sum(1 for p in sim.participants.values()
                         if not p.malicious and p.applied_commit == "C-EVIL")
        return total
    safe = evil_applied(1 / 3, 14)
    broken = evil_applied(0.7, 14)
    results.append(check("R14 <n/3 forges nothing; >2n/3 does",
                         safe == 0 and broken > 0,
                         f"honest-applied C-EVIL: mal=1/3 -> {safe}, "
                         f"mal=0.7 -> {broken}"))

    # R15: RFC concurrent minting forks at least as often as the primary-first
    # model. Every steward mints a distinct commit, so a member that misses the
    # winner selects a competitor -- the primary-first sim, where the backup
    # commits only on failure, understates this. Same loss, plain baseline.
    common = dict(scenario="plain", p_loss=0.2, n_stewards=3, n_members=9,
                  n_trials=400, seed=15)
    primary = run_trials(Config(**common))
    allmint = run_trials(Config(all_mint=True, **common))
    results.append(check("R15 all-mint forks >= primary-first",
                         allmint["forks"] >= primary["forks"],
                         f"primary forks={primary['forks']}, "
                         f"all-mint forks={allmint['forks']}"))

    # R16: the sync layer resolves the RFC all-mint fork. Over concurrent
    # minting, plain forks heavily, but reflexion2 -- converging on the winner
    # among the candidate set via attestation + body pull -- gets it to zero at
    # moderate loss, where the winner's existence still propagates.
    common = dict(all_mint=True, p_loss=0.3, n_stewards=3, n_members=9,
                  n_trials=300, seed=21)
    am_plain = run_trials(Config(scenario="plain", **common))
    am_sync = run_trials(Config(scenario="reflexion2", **common))
    results.append(check("R16 sync resolves all-mint fork",
                         am_plain["forks"] > 0 and am_sync["forks"] == 0
                         and am_sync["successes"] > am_plain["successes"],
                         f"plain S/F={am_plain['successes']}/{am_plain['forks']}, "
                         f"reflexion2 S/F={am_sync['successes']}/{am_sync['forks']}"))

    # R17: collection efficiency. All-mint costs more messages than primary-first
    # -- every steward broadcasts its own commit, so the commit traffic scales
    # with the steward count -- while converging in about the same time (the sync
    # latency is set by the attest+pull rounds, not by how many stewards minted).
    common = dict(scenario="reflexion2", p_loss=0.1, n_stewards=3, n_members=9,
                  n_trials=300, seed=21)
    pf = run_trials(Config(**common))
    am = run_trials(Config(all_mint=True, **common))
    slower = am["avg_convergence"] > 1.5 * pf["avg_convergence"]
    results.append(check("R17 all-mint costs more msgs, not more time",
                         am["avg_msgs"] > pf["avg_msgs"] and not slower,
                         f"msgs primary={pf['avg_msgs']:.0f} all-mint={am['avg_msgs']:.0f}; "
                         f"conv primary={pf['avg_convergence']:.1f}s "
                         f"all-mint={am['avg_convergence']:.1f}s"))

    print("-" * 60)
    if all(results):
        print(f"ALL {len(results)} REQUIREMENTS PASS")
        return 0
    else:
        n_fail = sum(1 for r in results if not r)
        print(f"{n_fail}/{len(results)} REQUIREMENTS FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
