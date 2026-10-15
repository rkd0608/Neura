# Soak reports

72-hour soaks of the simulated site with injected faults (design §17.7), one JSON report per run:
the faults injected, the invariant monitor's summary (every check it made and every violation),
availability, resources and memory per simulated hour.

Reproduce a run (about two hours on one core):

```bash
uv run neura-sim-soak --hours 72 --model act --merge replace --seed 1 --report act-replace.json
uv run neura-sim-soak --hours 72 --model smolvla --merge replace --seed 2 --report smolvla-replace.json
uv run neura-sim-soak --hours 72 --model act --merge blend --seed 3 --report act-blend.json
```

The weekly `Soak and chaos` workflow runs the same three and uploads their reports.

| Date | Commit | Run | Faults | Recoveries | Actions checked | Violations |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-10-15 | 30ac115 | ACT, replace merge | 4,339 | 1,350 | 30,226,689 | 0 |
| 2026-10-15 | 30ac115 | SmolVLA, replace merge | 4,425 | 1,479 | 30,150,473 | 0 |
| 2026-10-15 | 30ac115 | ACT, blend merge | 4,264 | 1,402 | 30,175,256 | 0 |
