# sds_pure_sim.py

Pure SDS-style simulation for de-MLS: ES and BS commit at the same time,
messages get lost (`p_loss`), and we measure whether the group converges.

Single file, no dependencies beyond the Python standard library.

## Run it

```bash
python3 sds_pure_sim.py
```

## Flags

| Flag | Default | What it does |
|---|---|---|
| `--n` | `100` | Number of participants (0=ES, 1=BS, rest members). |
| `--trials` | `50` | Trials per p_loss point. 300 is thorough but slow (~0.4-0.5s/trial at n=100). |
| `--max-time` | `60.0` | Simulation cutoff, in seconds. |
| `--es-afk` | off | ES never commits at all. Tests whether BS's fallback recovers the group. |

## Examples

```bash
# default: ES online, n=100, 50 trials per p_loss point (0.1-0.5)
python3 sds_pure_sim.py

# ES never shows up -- does BS's fallback save the group?
python3 sds_pure_sim.py --es-afk

# bigger group, more trials (slower)
python3 sds_pure_sim.py --n 1000 --trials 100

# quick smoke test
python3 sds_pure_sim.py --trials 10
```

## Reading the output

```
p_loss  success  avg_stall   fork  heavy_pub  light_pub  total_pub
```

- **success** — fraction of trials where *every* participant converged (strict, all-or-nothing).
- **avg_stall** — average number of participants still stuck per trial (the more informative number when success looks low but most of the group actually made it).
- **fork** — trials where participants disagreed on the applied commit. Should always be 0 by design.
- **heavy_pub / light_pub / total_pub** — average commit / sync / total broadcasts per trial (message cost).
