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
