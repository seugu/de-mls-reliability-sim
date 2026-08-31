# de-MLS Reliability Sim — Branches

Simulates de-MLS commit delivery under packet loss (no real MLS/de-MLS crypto), to compare sync mechanisms.

| Branch | What it tests |
|---|---|
| `plain` | Steward commits, BS falls back to its own commit if it misses the steward's, members can body-request what they're missing — but no quorum/decision layer, so competing commits can still fork. |
| `quorum` | Committee/attestation-based sync (2n/3 quorum, deterministic committee) to prevent forks. |
| `heartbeat-no-quorum` | No quorum at all — single steward heartbeats + periodic body push; optional backup steward (BS) takes over if the steward is afk. |
| Ekaterina's PR | Adds `apply_only_on_quorum`, `equivocate` (forged-commit resistance test), and `all_mint` (every steward commits independently, matching what the RFC actually allows) on top of the quorum model. |

Each branch has its own `report*.md` with the actual numbers (success/fork/stall rates, message costs) for that approach.
