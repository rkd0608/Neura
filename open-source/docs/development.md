# Development

## The repository

| Path | What it is |
|---|---|
| `packages/neura-runtime` | the runtime, transports, incident records (`neura_runtime`) |
| `packages/neura-robots` | robots: SO-101, ROS 2, simulated arms (`sim/`: Menagerie and ALOHA), the robot-side process (`neura_robots`) |
| `packages/neura-contracts` | contract documents and their compatibility check (`neura_contracts`) |
| `packages/neura-proto` | the wire protocol, generated from `proto/neura/v1` (`neura.v1`) |
| `examples/` | contracts, robot configurations, example policies, the quickstart |
| `scripts/doctor.py` | `make doctor`: what this machine can run, and how to get the rest |
| `docs/` | these pages |

They are one distribution, `neura-robot`; each package's code is in its `src/` and its tests in
its `tests/`. Each module's first lines say what it is for.

Comments that cite `design §N` refer to sections of the design document of Neura, the project
neura-robot comes from. That document isn't published; what matters here is in these docs, and a
comment's own text should stand without it. If one doesn't, that's worth an issue.

## Setting up

```bash
python -m venv .venv && . .venv/bin/activate
make setup        # or make setup-all for every extra (Python 3.12+)
make doctor       # what this machine can run, and how to get the rest
make check
```

`make help` lists the tasks: `setup`, `setup-all`, `assets` (the simulated arms' models),
`doctor`, `lint`, `format`, `typecheck`, `test`, `quickstart`, `check` (as CI runs them),
`test-docker`. The README's [set-up section](../README.md#set-up-your-machine) covers every
prerequisite, Hugging Face included.

## Tests

- `make test` runs everything this machine can. Each skipped test says what it needs.
- **Without hardware.** The SO-101 tests run LeRobot's real servo driver against an emulated servo
  bus on a pseudo-terminal (`neura_robots.emulator`; `pip install -e ".[so101]"`, Python 3.12+).
  The simulated arms run MuJoCo Menagerie's models (`neura_robots.sim`; the `sim` extra, git and
  offscreen OpenGL). ROS 2 tests run ROS 2 Jazzy in Docker. Policy-server tests run openpi's and
  LeRobot's own servers.
- **Docker tests.** `make test-docker` builds the openpi and ROS 2 test images and runs those tests.
  The ROS 2 test drops packets with nftables inside a container, so Docker must allow
  `--cap-add NET_ADMIN`.
- **Warnings are errors** in tests, and timing-sensitive tests assume the machine isn't busy with
  something else: run them alone when in doubt.

## CI

Every pull request runs `make check` on Python 3.10 to 3.13, on x86-64 and arm64 runners, plus the
extras (LeRobot, MuJoCo) on Python 3.12, the Docker tests, and a build of the wheel that is
installed and smoke-tested from scratch.

## Regenerating the protocol

`packages/neura-proto/src/neura/v1/*_pb2*.py` are generated; never edit them by hand. After changing
`proto/neura/v1/*.proto`:

```bash
pip install grpcio-tools==1.84.0
python -m grpc_tools.protoc -I proto --python_out=packages/neura-proto/src \
    --grpc_python_out=packages/neura-proto/src --pyi_out=packages/neura-proto/src proto/neura/v1/*.proto
```

## Adding a robot

Implement the `Arm` interface (`packages/neura-robots/src/neura_robots/arm.py`): what the runtime
needs (authority, commands, fault responses, sensor snapshots) plus recovery and revalidation.
Give it a configuration `type` in `neura_robots.cli`, and a test that runs it through
`ArmProcess` with the invariant monitor at zero violations; `tests/test_process.py` (the SO-101),
`tests/test_ros2.py` and `tests/test_menagerie.py` are the models. A simulated robot is a
`neura_robots.sim.scene.Scene`: its commands, cameras and control period, from which its robot
profile is made; `sim/menagerie.py` is the example.

## Adding a policy transport

Implement `PolicyClient` (`packages/neura-runtime/src/neura_runtime/policy.py`), add a URI scheme in
`neura_robots.policies`, and add it as a case of the scenario in
`packages/neura-runtime/tests/test_local.py`. Test against the real server, not a mock.
