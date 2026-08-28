from demls_sim import Config, run_trials

# Safe window (10s): BS's own miss-everything probability is negligible.
c_safe = Config(scenario="hbclose", p_loss=0.5, n_stewards=2, n_members=10,
                 hb_interval=1.0, hb_window=10.0, max_time=60.0,
                 n_trials=500, seed=7)
o_safe = run_trials(c_safe, es_afk_prob=0.0)
print("ES online, p=0.5, window=10s, 500 trials:",
      "success=%.3f fork=%d stall=%d" % (
          o_safe["success_rate"], o_safe["forks"], o_safe["stalls"]))

# Short window (3s): BS's own miss-everything probability is ~12.5% at
# p=0.5 (0.5**3) -- large enough to actually trigger the fork risk: BS can
# conclude "ES is afk" and mint C-BS even though ES is genuinely online,
# causing some members (who got C-ES normally) to diverge from those who
# followed BS into a needless phase 2.
c_short = Config(scenario="hbclose", p_loss=0.5, n_stewards=2, n_members=10,
                  hb_interval=1.0, hb_window=3.0, max_time=60.0,
                  n_trials=500, seed=7)
o_short = run_trials(c_short, es_afk_prob=0.0)
print("ES online, p=0.5, window=3s,  500 trials:",
      "success=%.3f fork=%d stall=%d" % (
          o_short["success_rate"], o_short["forks"], o_short["stalls"]))
