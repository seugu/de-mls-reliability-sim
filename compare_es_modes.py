from demls_sim import Config, run_trials

print("n=1000, hb_window=10s (safe), ES online vs ES afk")
print(f"{'p':>5} {'mode':>10} {'success':>8} {'fork':>5} {'stall':>6} {'msgs':>8}")
for p in [0.1, 0.3, 0.5]:
    c = Config(scenario="hbclose", p_loss=p, n_stewards=2, n_members=997,
               hb_interval=1.0, hb_window=10.0, max_time=60.0, n_trials=50, seed=5)
    o_on = run_trials(c, es_afk_prob=0.0)
    o_off = run_trials(c, es_afk_prob=1.0)
    print(f"{p:>5.1f} {'online':>10} {o_on['success_rate']:>8.2f} {o_on['forks']:>5} "
          f"{o_on['stalls']:>6} {o_on['avg_msgs']:>8.0f}")
    print(f"{p:>5.1f} {'afk':>10} {o_off['success_rate']:>8.2f} {o_off['forks']:>5} "
          f"{o_off['stalls']:>6} {o_off['avg_msgs']:>8.0f}")
