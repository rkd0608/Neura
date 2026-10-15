# Hardware qualification reports

One directory per arm; each report is the JSON `neura-qualify run` writes (design §7.1). A
combination is promoted to Supported (`neura-qualify promote`) only from a complete hardware
report that passed, with closed-loop episodes that showed non-inferiority (`neura-qualify
episodes`).

| Report | Arm | Scenarios | Parity (max abs error) | Invariant violations |
| --- | --- | --- | --- | --- |
| `emulated/2026-10-15.json` | emulated SO-101 (six STS3215 servos on a pseudo-terminal), synthetic cameras, synthetic ACT checkpoint on CPU | 21 of 21 passed | 1.5e-5 over 10 chunks (tolerance 1e-3) | 0 |

The emulated report shows that the harness and the integration work end to end through LeRobot's
real bus driver. It is not hardware evidence and can never be promoted.

Reproduce it (about 2 minutes, needs `make models-setup`):

```bash
make qualify-emulated
```

On hardware, see the docs site's hardware qualification page for the safety setup, then:

```bash
neura-qualify run --arm-config so101-arm.yaml --artifact ... --robot-profile ... \
  --execution-profile ... --checkpoint ./checkpoint --device cuda:0 --manual \
  --out qualification/reports/<arm>/<date>.json
neura-qualify episodes --arm-config so101-arm.yaml ... --out qualification/reports/<arm>/<date>-episodes.json
```
