# Contributing to neura-robot

Thanks for helping. neura-robot decides what a real robot executes, so the bar is the same for
every change, small or large: **it is proven by a test, and it makes no safety rule weaker.**

## Ways to help

- **Report a bug** with the [bug template](https://github.com/rkd0608/neura-robot/issues/new/choose).
  If a robot misbehaved, attach its incident record (`incidents/*.mcap`) if you can: it replays
  the runtime's decisions exactly. Records hold joint states and timings, never images unless you
  turned them on, so check before attaching one from a sensitive site.
- **Add a robot or a policy server.** A new robot implements the `Arm` interface
  (`packages/neura-robots/src/neura_robots/arm.py`); a new policy server implements
  `PolicyClient` (`packages/neura-runtime/src/neura_runtime/policy.py`). Both must pass the
  end-to-end scenario in `packages/neura-runtime/tests/test_local.py`.
- **Improve the docs.** If something took you longer than it should have, that is a docs bug.

For anything bigger than a bug fix, please open an issue first to agree on the approach.

## Set up

Linux or macOS, Python 3.10 or newer:

```bash
git clone https://github.com/rkd0608/neura-robot && cd neura-robot
python -m venv .venv && . .venv/bin/activate
make setup            # pip install -e ".[dev,openpi]"; make setup-all for every extra
make doctor           # what this machine can run, and how to get the rest
make check            # lint, types and tests, as CI runs them
```

The README's [set-up section](README.md#set-up-your-machine) lists every prerequisite (Docker,
OpenGL, Hugging Face) and what each unlocks.

With [uv](https://docs.astral.sh/uv/): `uv venv && uv pip install -e ".[dev,openpi]"`.

Extras for the parts that need them:

| Extra | For | Notes |
|---|---|---|
| `so101` | the SO-101 arm and its emulator | LeRobot; Python 3.12+ |
| `lerobot` | the LeRobot policy-server transport | LeRobot; Python 3.12+ |
| `sim` | the simulated arms (MuJoCo Menagerie) and ALOHA, in MuJoCo | git and offscreen OpenGL |
| `openpi` | the openpi policy-server transport | |

## Tests

```bash
make test                       # everything that runs here; what it skips says why
pytest packages/neura-runtime   # one package
pytest -k incident              # by name
```

Some tests need more than Python, and skip with a reason when it is missing:

- **LeRobot and the SO-101 emulator**: `pip install -e ".[so101,lerobot]"` (Python 3.12+).
- **MuJoCo**: `pip install -e ".[sim]"`, git (the simulated arms' models are fetched once; `make
  assets`), and headless OpenGL to render its cameras (on Debian or
  Ubuntu: `sudo apt-get install libegl1 libgl1`; or set `MUJOCO_GL=osmesa` with `libosmesa6`).
- **openpi's own policy server** (Docker):
  `docker build -t neura-openpi-test:dev packages/neura-runtime/tests/openpi` and
  `NEURA_TEST_OPENPI_IMAGE=neura-openpi-test:dev pytest packages/neura-runtime/tests/test_local.py`.
- **ROS 2 Jazzy under packet loss** (Docker):
  `docker build -t neura-ros2-test:dev packages/neura-robots/tests/ros2` and
  `NEURA_TEST_ROS2_IMAGE=neura-ros2-test:dev pytest packages/neura-robots/tests/test_ros2.py`.

`make test-docker` builds both images and runs both. A skipped test proves nothing: if your change
touches what it covers, run it.

## What a good change looks like

- **A bug gets a failing test first**, the smallest one that fails without the fix, in the test
  file that already covers that code. Prefer extending a test to writing a new one.
- **Find the cause.** No retries, sleeps, swallowed exceptions or loosened assertions to make a
  test pass; fix the code where the mistake is made.
- **Safety rules are never relaxed to make something work.** If a rule is wrong, say why in the
  issue first. End-to-end tests run an independent invariant monitor
  (`neura_runtime.invariants`); a change must leave it at zero violations.
- **Match the code around you.** Ruff (100 columns), mypy strict, Python 3.10 syntax (CI checks
  3.10 to 3.13), tests with warnings as errors. A new module starts with a docstring whose first
  sentence says what it is for.
- **Keep files small**: under 800 lines.
- **Docs move with the code.** A change people can see updates `docs/` and, if it is notable,
  `CHANGELOG.md`.

## Pull requests

1. Fork, branch from `main`, make the change with its test.
2. `make check` passes locally.
3. Open the pull request and fill in the template: what changed, why, how it was proven.
4. Sign off your commits (`git commit -s`), certifying the
   [Developer Certificate of Origin](https://developercertificate.org/): you wrote the change or
   have the right to contribute it under Apache-2.0.

**How changes land.** neura-robot is developed inside Neura's main repository and published here
by a one-way mirror. A maintainer applies an accepted pull request there, credits you as its
author, and it comes back here with the next mirror; your pull request is then closed with a link
to the commit. Your authorship and sign-off are kept.

## Conduct and security

Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md). Security problems are
reported privately, never in issues: see [SECURITY.md](SECURITY.md).
