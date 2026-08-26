#!/usr/bin/env python3
"""
Reproduce every comparison table in FINDINGS.md.

The self-checks in test_demls.py assert the *claims* (pass/fail). This script
prints the *measurements* behind them, with the same seeds and trial counts, so
the numbers in FINDINGS.md are reproducible. Analytic tables (committee sizing)
need no simulation and are computed directly.

    python3 measurements.py            # findings 1-4 (fast, n<=120)
    python3 measurements.py --scale    # also the n=1000 scale check (minutes)
"""

import sys
from math import lgamma, log, exp, ceil
from demls_sim import Config, run_trials


def sfstat(o):
    return f'{o["successes"]:>3}/{o["forks"]:>3}/{o["stalls"]:>3}'


# --------------------------------------------------------------------------
# Finding 1 — collection model: RFC concurrent minting forks the default
# --------------------------------------------------------------------------
def finding1():
    print("== Finding 1: forks, plain baseline, n=12 (seed 15, 400 trials) ==")
    print(f'{"p_loss":>6} | {"primary forks":>13} | {"all-mint forks":>14}')
    for p in [0.05, 0.10, 0.20, 0.30]:
        c = dict(scenario="plain", p_loss=p, n_stewards=3, n_members=9,
                 n_trials=400, seed=15)
        pf = run_trials(Config(**c))
        am = run_trials(Config(all_mint=True, **c))
        print(f'{p:>6} | {pf["forks"]:>13} | {am["forks"]:>14}')

    print("\n   steward-count dial (all-mint, n=12, p=0.2, seed 15):")
    print(f'{"sn":>4} | {"forks":>5}')
    for sn in [1, 2, 3, 4, 6]:
        o = run_trials(Config(scenario="plain", all_mint=True, p_loss=0.2,
                              n_stewards=sn, n_members=12 - sn,
                              n_trials=400, seed=15))
        print(f'{sn:>4} | {o["forks"]:>5}')


# --------------------------------------------------------------------------
# Finding 2 — a sync layer over the candidate set resolves it
# --------------------------------------------------------------------------
def finding2():
    print("\n== Finding 2: all-mint, n=12, success/fork/stall (seed 21, 300 trials) ==")
    print(f'{"p":>4} | {"plain":>11} | {"reflexion2":>11} | {"committee":>11} | {"quorum-gate":>11}')
    for p in [0.1, 0.2, 0.3, 0.5]:
        base = dict(all_mint=True, p_loss=p, n_stewards=3, n_members=9,
                    n_trials=300, seed=21)
        pl = run_trials(Config(scenario="plain", **base))
        r2 = run_trials(Config(scenario="reflexion2", **base))
        cm = run_trials(Config(scenario="committee", **base))
        qg = run_trials(Config(scenario="reflexion2", apply_only_on_quorum=True, **base))
        print(f'{p:>4} | {sfstat(pl)} | {sfstat(r2)} | {sfstat(cm)} | {sfstat(qg)}')


# --------------------------------------------------------------------------
# Finding 3 — cost is bandwidth, not time
# --------------------------------------------------------------------------
def finding3(scale):
    print("\n== Finding 3: efficiency, n=12 (seed 21) ==")

    def conv(o):
        return f'{o["avg_convergence"]:.1f}s' if o["avg_convergence"] is not None else '  -'
    for p in [0.1, 0.2]:
        c = dict(scenario="reflexion2", p_loss=p, n_stewards=3, n_members=9,
                 n_trials=300, seed=21)
        pf = run_trials(Config(**c))
        am = run_trials(Config(all_mint=True, **c))
        print(f'  reflexion2 p={p}: primary msgs={pf["avg_msgs"]:.0f} conv={conv(pf)} '
              f'succ={pf["successes"]} | all-mint msgs={am["avg_msgs"]:.0f} '
              f'conv={conv(am)} succ={am["successes"]}')

    print("   commit-msg scaling with steward count (plain, n=12, p=0.1, seed 21):")
    print(f'{"sn":>4} | {"primary":>7} | {"all-mint":>8}')
    for sn in [1, 3, 6]:
        cc = dict(scenario="plain", p_loss=0.1, n_stewards=sn,
                  n_members=12 - sn, n_trials=200, seed=21)
        pf = run_trials(Config(**cc))
        am = run_trials(Config(all_mint=True, **cc))
        print(f'{sn:>4} | {pf["avg_msgs"]:>7.0f} | {am["avg_msgs"]:>8.0f}')

    if scale:
        print("\n   scale check, all-mint, n=1000, sn=3 (slow):")
        print(f'{"sync":>12} | {"p":>5} | {"succ":>5} | {"msgs":>9} | {"conv":>5}')
        for sc, tr in [("committee", 5), ("reflexion2", 3)]:
            for p in [0.05, 0.1]:
                o = run_trials(Config(scenario=sc, all_mint=True, p_loss=p,
                                      n_stewards=3, n_members=997,
                                      n_trials=tr, seed=7))
                cv = f'{o["avg_convergence"]:.1f}s' if o["avg_convergence"] else '-'
                print(f'{sc:>12} | {p:>5} | {o["successes"]:>2}/{tr:<2} | '
                      f'{o["avg_msgs"]:>9.0f} | {cv:>5}')


# --------------------------------------------------------------------------
# Finding 4 — committee sizing (analytic + empirical)
# --------------------------------------------------------------------------
def _tail_unsafe(k, f):
    # P(committee of k >= 1/3 Byzantine), large-population (binomial) model.
    thr = ceil(k / 3)
    terms = [lgamma(k + 1) - lgamma(i + 1) - lgamma(k - i + 1)
             + i * log(f) + (k - i) * log(1 - f) for i in range(thr, k + 1)]
    if not terms:
        return 0.0
    m = max(terms)
    return exp(m) * sum(exp(t - m) for t in terms)


def _min_k(f, eps, cap=5000):
    for k in range(1, cap + 1):
        if _tail_unsafe(k, f) <= eps:
            return k
    return f'>{cap}'


def finding4(seed=31):
    print("\n== Finding 4a: min committee SIZE k for P(>=1/3 Byz) <= eps (analytic) ==")
    print(f'{"Byz f":>6} | {"1e-3":>6} | {"1e-6":>6} | {"1e-9":>6} | {"1e-12":>6}')
    for f in [0.10, 0.15, 0.20, 0.25, 0.30]:
        cells = ' | '.join(f'{str(_min_k(f, e)):>6}' for e in [1e-3, 1e-6, 1e-9, 1e-12])
        print(f'{f:>6.2f} | {cells}')

    print("\n== Finding 4b: percent is wasteful (analytic, f=0.2, fixed k=230) ==")
    f, K = 0.20, 230
    print(f'{"n":>8} | {"20% k":>6} | {"P_unsafe":>9} | {"msgs 20%":>13} | '
          f'{"msgs k=230":>12} | {"waste":>6}')
    for n in [1000, 5000, 10000, 100000]:
        kp = round(0.2 * n)
        print(f'{n:>8} | {kp:>6} | {_tail_unsafe(kp, f):>9.0e} | {kp*n:>13,} | '
              f'{K*n:>12,} | {kp*n/(K*n):>5.1f}x')

    print("\n== Finding 4c: liveness under loss, forks/150 (n=120, all-mint committee, seed 31) ==")
    n = 120
    ks = [2, 3, 4, 6, 9, 12, 18, 24, 36]
    ps = [0.1, 0.2, 0.3, 0.5, 0.7]
    print("  k\\p | " + " | ".join(f"{p:>5}" for p in ps))
    grid = {}
    for k in ks:
        cells = []
        for p in ps:
            o = run_trials(Config(scenario="committee", all_mint=True, p_loss=p,
                                  n_stewards=3, n_members=n - 3,
                                  committee_ratio=k / n, n_trials=150, seed=seed))
            grid[(k, p)] = o["forks"]
            cells.append(f"{o['forks']:>5}")
        print(f'{k:>5} | ' + " | ".join(cells))
    print("  min committee k for 0 forks, per loss:")
    for p in ps:
        mink = next((k for k in ks if grid[(k, p)] == 0), f'>{ks[-1]}')
        print(f'    p={p}: k>={mink}')


if __name__ == "__main__":
    scale = "--scale" in sys.argv
    finding1()
    finding2()
    finding3(scale)
    finding4()
    print("\n=== done ===")
