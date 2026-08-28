from demls_sim import Config, run_trials
import time

t0 = time.time()
c = Config(scenario="hbclose", p_loss=0.4, n_stewards=1, n_members=997,
           hb_interval=1.0, hb_window=10.0, max_time=60.0, n_trials=300, seed=5)
o = run_trials(c, es_afk_prob=0.0)
print(f"n=1000, p_loss=0.4, hb_window=10s, 300 trials")
print(f"success={o['successes']}/300 ({o['success_rate']*100:.1f}%) "
      f"fork={o['forks']} stall={o['stalls']} ({time.time()-t0:.0f}s)")
