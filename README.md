<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset=".github/assets/banner-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset=".github/assets/banner-light.svg">
  <img alt="Neura by NucleiLabs: run your robot policies across your fleet, on your GPUs." src=".github/assets/banner-dark.svg" width="100%">
</picture>

<p>
  <a href="https://github.com/rkd0608/neura/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/rkd0608/neura/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <img alt="Python 3.13" src="https://img.shields.io/badge/python-3.13-7c63ff?logo=python&logoColor=white">
  <img alt="TypeScript" src="https://img.shields.io/badge/console-React%20%2B%20TypeScript-7c63ff?logo=typescript&logoColor=white">
  <img alt="LeRobot" src="https://img.shields.io/badge/policies-LeRobot%20%C2%B7%20Python%20%C2%B7%20containers-7c63ff">
  <img alt="Status" src="https://img.shields.io/badge/status-in%20development-f2b84b">
</p>

<p>
  <a href="#-the-60-second-tour"><b>Tour</b></a> ·
  <a href="#-how-it-works"><b>How it works</b></a> ·
  <a href="#-quickstart"><b>Quickstart</b></a> ·
  <a href="#-whats-inside"><b>What's inside</b></a> ·
  <a href="site/docs"><b>Docs</b></a> ·
  <a href="docs/neura-design.md"><b>Design</b></a>
</p>

</div>

---

**NucleiLabs builds infrastructure that makes robot AI easier to run.** **Neura**, our first
product, deploys trained robot policies to many robots from shared GPU servers at the customer's
own site. It times inference around each robot's control loop, makes every model change a safe,
reviewable rollout, and records what robots do, so a better model can be trained, tested against
the running one, and released through the same flow.

> [!NOTE]
> Neura is in development. Nothing here is a released product. Every claim below links to the
> test, benchmark or report that backs it.

## ✨ Why Neura

<table>
<tr>
<td width="33%" valign="top">

### 🚀 Deploy like software
Push a policy, and Neura checks it fits the robot, the site times it on its own GPU, a shadow
test compares it with what runs today, and robots switch **between tasks**. Roll back in one
command.

</td>
<td width="33%" valign="top">

### 🛡️ Safe by construction
**No cloud credential can start motion.** A person at the site approves every activation. Each
robot checks every action chunk before anything moves, and the site keeps running when the cloud
is unreachable.

</td>
<td width="33%" valign="top">

### 🧠 Any policy
**LeRobot** checkpoints (ACT, Diffusion Policy, SmolVLA, π0, π0.5), your own **Python** class with
`@neura.policy`, or a proprietary stack in a **container**. One declaration describes them all.

</td>
</tr>
<tr>
<td valign="top">

### ⚡ Share the GPU
Batching, LoRA multi-adapter batching and co-located fine-tunes serve many robots per device,
inside each robot's deadline. On one NVIDIA L4, 32 to 96 robots per device met the SLO, against
one GPU per robot ([report](bench/reports/nvidia-l4/README.md), losing cases included).

</td>
<td valign="top">

### 🏭 Your data stays home
Camera frames, robot state, actions and weights stay at the site. The cloud holds records only,
and everything a site sends is in a hash-chained outbound ledger. Weights can be sealed with your
own key.

</td>
<td valign="top">

### 🔁 Gets better
Robots record episodes at the site, fine-tunes run on the site's GPUs, and a candidate must be
**no worse** than the running model in simulation, replay and shadow before it may replace it.

</td>
</tr>
</table>

## ⏱ The 60-second tour

From a fresh workspace to a simulated arm running your policy, a new version and a rollback:

```console
$ neura login                                   # browser sign-in, or a token for CI
$ neura site create lab --bench
$ neura site join lab --simulate 1              # this machine becomes the site's server

$ neura policy push hold.py --name hold

  Pushing hold

  ✓ Fits so101-follower         reads state; returns 10 actions
  ✓ Signed                      ed25519:a1d97c56847be359
  ✓ Files at lab                2 added, 0 already there (this machine)

  hold v1  python · 560 B

$ neura try hold --on sim-1

  Trying hold v1 on sim-1

  ✓ Fits the robots       fits 1 so101-follower robot: 30 Hz, 10-action chunks
  ✓ At the site           weights verified; under 1 ms per pass (measured); next chunk asked 3 actions early
  – Shadow test           not asked for
  ✓ Approval              approved by the bench site's agent
  ✓ Switch                sim-1 switched

  Live  hold v1

$ neura deploy hold --to sim-1                  # v2: check, prepare, shadow, approve, switch
$ neura rollback sim-1                          # back to v1, between tasks
```

The policy is any class with `act(observation)`:

```python
import numpy as np
import neura


@neura.policy(chunk_length=10, cameras={})
class Hold:
    """Holds the arm where it is."""

    def act(self, observation):
        return np.tile(observation["observation.state"], (10, 1))
```

The same run is a test against a live control plane and a real site agent:
[`test_neura_cli.py`](packages/neura-control/tests/test_neura_cli.py). Production servers install
the agent with one line, which the console shows with a single-use token:

```bash
curl -fsSL https://<your-neura>/install.sh | sudo sh -s -- --token nen_...
```

## 🧭 How it works

```mermaid
flowchart LR
    subgraph cloud["☁️ Neura cloud: records only"]
        API["Control plane<br/>API · audit · rollouts"]
        UI["Console<br/>Fleet · Policies · Rollouts · Settings"]
        CLI["neura CLI"]
    end
    subgraph site["🏭 Customer site: on the action path"]
        AGENT["Site agent<br/>signed desired state · local approval"]
        GW["Gateway<br/>leases · admission · deadline scheduler"]
        W1["Policy worker<br/>GPU 0"]
        W2["Policy worker<br/>GPU 1"]
    end
    subgraph robots["🤖 Robots"]
        R1["Robot runtime<br/>action queue · fencing · fault response"]
        R2["Robot runtime"]
    end
    UI --> API
    CLI --> API
    API <-->|"mTLS, outbound from the site"| AGENT
    AGENT --> GW
    GW <--> W1
    GW <--> W2
    R1 <-->|"observations ⇄ action chunks"| GW
    R2 <--> GW
```

- **Four things to know.** A **site** is where robots are: a *bench* for trying things, or
  *production*. Its server finds the **robots**. A **policy** has numbered versions. A **rollout**
  changes what robots run.
- **The cloud is never on the action path.** Robots talk to the gateway on the site's own network.
  The control plane holds desired state and history; the site keeps serving within limits the
  customer sets if the cloud goes away.
- **The robot decides what it executes.** Every chunk must be fresh, for the current task and from
  the robot's own session, or the robot holds and faults safely.
- **Every rollout has the same steps:** fits the robots → prepared and timed at the site → shadow
  test → approval at the site → switch at the next task boundary.

## 🚀 Quickstart

You need Python 3.13, [uv](https://docs.astral.sh/uv/), Node 22 with pnpm, and Docker with Compose.

```bash
git clone https://github.com/rkd0608/neura.git && cd neura
make setup     # the Python workspace, into .venv
make dev       # Postgres, control plane, site agent, simulated robots, console on :3000
```

Then sign in with the development token and run the tour against your local stack:

```bash
grep -o 'ntk_tok_[A-Za-z0-9_-]*' deploy/compose/compose.yaml \
  | uv run neura login --api http://127.0.0.1:8080 --with-token
uv run neura site create lab --bench
uv run neura site join lab --simulate 1 --mock-policies --control-plane 127.0.0.1:8081
uv run neura policy push ./my_policy.py --name pick
uv run neura try pick --on sim-1
```

The full walkthrough is in the docs: [Deploy a policy](site/docs/deploy-a-policy.md) and
[Bring your policy](site/docs/policies.md).

## 📦 What's inside

<table>
<tr><th align="left">Product</th><th align="left"></th></tr>
<tr><td><a href="packages/neura"><code>packages/neura</code></a></td><td>The <code>neura</code> command and <code>@neura.policy</code>: sign in, sites, robots, push, deploy, approve, rollback</td></tr>
<tr><td><a href="console"><code>console/</code></a></td><td>Web console: React + TypeScript, client generated from the API spec, end-to-end tests in Chromium</td></tr>
<tr><td><a href="landing"><code>landing/</code></a> · <a href="site"><code>site/</code></a></td><td>The NucleiLabs website, and the developer docs it serves at <code>/docs/</code></td></tr>
<tr><th align="left">Cloud</th><th></th></tr>
<tr><td><a href="packages/neura-control"><code>neura-control</code></a></td><td>Control plane: management API on Postgres, auth and SSO, workspaces, rollouts, audit, events</td></tr>
<tr><td><a href="packages/neura-sdk"><code>neura-sdk</code></a></td><td>Python client for the management API, generated from <a href="api/openapi.yaml"><code>api/openapi.yaml</code></a></td></tr>
<tr><th align="left">Site</th><th></th></tr>
<tr><td><a href="packages/neura-agent"><code>neura-agent</code></a></td><td>Site agent: enrollment, signed desired state, local approval, weights, upgrades, robot discovery</td></tr>
<tr><td><a href="packages/neura-server"><code>neura-server</code></a></td><td>Data plane: gateway, leases, admission control, deadline scheduler, policy workers, shadow serving, capture</td></tr>
<tr><td><a href="packages/neura-adapters"><code>neura-adapters</code></a></td><td>Model adapters: LeRobot ACT, Diffusion, SmolVLA, π0, π0.5; Python and container policies; LoRA batching; profiler</td></tr>
<tr><td><a href="packages/neura-learn"><code>neura-learn</code></a></td><td>At-site evaluation and fine-tuning: simulation, replay and shadow suites, non-inferiority verdicts</td></tr>
<tr><th align="left">Robot</th><th></th></tr>
<tr><td><a href="packages/neura-runtime"><code>neura-runtime</code></a></td><td>Robot runtime: action queue, chunk merging, fencing, the robot's state machine</td></tr>
<tr><td><a href="packages/neura-robots"><code>neura-robots</code></a></td><td>Real arms: SO-101 over LeRobot, a servo-bus emulator, <code>neura-arm</code>, <code>neura-qualify</code></td></tr>
<tr><th align="left">Foundations</th><th></th></tr>
<tr><td><a href="packages/neura-contracts"><code>neura-contracts</code></a></td><td>Model, robot and execution-profile contracts, the compatibility check, signing</td></tr>
<tr><td><a href="proto/neura/v1"><code>proto/</code></a> · <a href="packages/neura-proto"><code>neura-proto</code></a></td><td>The wire protocol (robot ⇄ gateway, site ⇄ cloud, gateway ⇄ worker) and its bindings</td></tr>
<tr><td><a href="packages/neura-sim"><code>neura-sim</code></a> · <a href="packages/neura-bench"><code>neura-bench</code></a></td><td>Simulated sites, soak and chaos with an invariant monitor; the GPU-sharing benchmark</td></tr>
<tr><td><a href="deploy"><code>deploy/</code></a></td><td>Container images, the local Compose stack, the installer, Google Cloud deployment, a customer site kit</td></tr>
</table>

## 📊 Evidence

| What | Result | Where |
| --- | --- | --- |
| GPU sharing, NVIDIA L4 | 32 robots on one shared policy, 96 on different fine-tunes, inside the deadline SLO; on this device, plain co-location matched or beat batching | [bench/reports/nvidia-l4](bench/reports/nvidia-l4/README.md) |
| Vision-language-action models | SmolVLA and π0 measured on the same device, including where sharing does not pay | [bench/reports/nvidia-l4-vla](bench/reports/nvidia-l4-vla/README.md) |
| Soak | 72 simulated hours with injected faults, no invariant violated | [soak/reports](soak/reports) |
| Hardware qualification | The SO-101 fault suite and parity on the emulated arm (real hardware pending) | [qualification/reports](qualification/reports) |
| End to end | The CLI tour, the console's four pages, onboarding to approval, all in CI | [CI](.github/workflows/ci.yml) |

## 🛠 Development

```bash
make check            # lint, strict types, generated files, tests: what CI runs
make console-check    # the console: format, lint, types, unit tests
make console-e2e      # the console's journeys in Chromium, against the real backend
make docs             # the docs on http://127.0.0.1:3000/docs/
make landing-dev      # the website on http://127.0.0.1:5174
make models-setup     # PyTorch and LeRobot (about 5 GB), for real models
make test-models      # adapters, real workers, the benchmark, the SO-101 integration
make soak             # 72 simulated hours with injected faults
make chaos            # faults in a real gateway, its workers and the network
```

Control-plane tests need Postgres: `docker compose -f deploy/compose/compose.yaml up -d postgres`,
then `NEURA_TEST_DATABASE_URL=postgresql://neura:neura@127.0.0.1:5432/neura make test`.

<details>
<summary><b>Operating a site by hand</b>: the agent CLI, weights, signing, sealing, telemetry</summary>

On the server (the `site-agent` container in the dev stack), the operator uses the agent CLI:

```bash
docker compose -f deploy/compose/compose.yaml exec site-agent neura-agent status
docker compose -f deploy/compose/compose.yaml exec site-agent neura-agent approvals
docker compose -f deploy/compose/compose.yaml exec \
  -e NEURA_AGENT_OPERATOR_TOKEN=ntop_dev_operator_public_token \
  site-agent neura-agent approve <command-id> --operator you@site
```

Weights stay at the site. Import a checkpoint, and `prepare` loads and measures it on the GPU:

```bash
neura-agent import-weights /path/to/checkpoint --artifact my-model.artifact.yaml
```

A site runs an artifact only if a publisher key its operator trusts signed it:

```bash
uv run neura-contracts publisher-key --out release.pem
uv run neura-contracts sign my-model.artifact.yaml --key release.pem
neura-agent trust-publisher <public key>                     # on the server
```

To keep weights in a store you do not fully control, seal them with your own key first:

```bash
uv run neura-contracts key-file --out weights.kek
uv run neura-contracts seal /path/to/checkpoint --out sealed/ --key-file weights.kek
```

Everything a site sends is in its hash-chained outbound ledger. `NEURA_AGENT_TELEMETRY_MODE=minimal`
sends only health, versions, identifiers and states:

```bash
docker compose -f deploy/compose/compose.yaml exec site-agent neura-agent outbound --summary
docker compose -f deploy/compose/compose.yaml exec site-agent neura-agent outbound --verify
```

Prometheus and Grafana with Neura's dashboards (http://127.0.0.1:3001):

```bash
docker compose -f deploy/compose/compose.yaml --profile observability up -d
```

</details>

<details>
<summary><b>Installing the agent without the one-liner</b></summary>

On a GPU server (Linux, Python 3.13+, openssl 3; Docker and the NVIDIA container toolkit for the
worker sandbox), with a signed release bundle and an enrollment token from the console:

```bash
sudo deploy/install/install.sh --bundle neura-agent-<version>.tar \
  --release-keys release-keys.pem --control-plane <host:port> --token <nen_...>
```

Bundles are built and signed with `tools/build_agent_release.py`, and verified before anything is
installed. The one-line installer runs when the control plane offers a release
(`NEURA_CONTROL_AGENT_RELEASE_VERSION`, `_URL`, `_SHA256`, `_KEYS_FILE`). Later upgrades arrive
signed through the control plane, if the site allows them, and roll back by themselves when the
new release is not healthy.

</details>

<details>
<summary><b>Deploying the cloud side</b> on Google Cloud</summary>

The control plane, console, website and docs run on one e2-micro VM with Postgres in a container
(or Cloud SQL), Secret Manager and Artifact Registry, about $8–15 a month:

```bash
NEURA_GCP_PROJECT=<project-id> NEURA_DOMAIN=<domain> deploy/gcp/deploy.sh up
deploy/gcp/deploy.sh admin-token    # the first admin API token
deploy/gcp/deploy.sh release        # ship the current commit
deploy/gcp/deploy.sh pause          # stop the VMs; data kept
```

Or run Actions → Deploy with the service account key as the `GCP_SA_KEY` secret. Point the domain
and its `console.`, `agents.` and `enroll.` names at the address `up` prints.

</details>

<details>
<summary><b>Being the customer</b>: the site kit</summary>

[`deploy/site-demo`](deploy/site-demo/README.md) runs a customer's site on any machine with
Docker, CPU only, connected to your Neura: a server and two simulated ALOHA robots running
LeRobot's published ACT policy, with a page at `http://127.0.0.1:8095` to watch them.

```bash
cd deploy/site-demo
./site.sh publish 'https://console.<domain>/o/<org>/p/<project>'
./site.sh connect '<the install command from the console>'
```

`deploy/gcp/site.sh test` runs the same flow from a server in its own network and region, with
latency, loss and a partition on the site's uplink.

</details>

## 📐 Conventions

- **Generated files are committed and checked.** Edit the source (`proto/`, `catalog/`, contract
  models), run `make gen`, commit both. CI fails on stale output.
- **Spec-first API.** [`api/openapi.yaml`](api/openapi.yaml) is the contract; the service exposes
  no route the spec does not declare.
- **Reason codes come from the catalog.** Every refusal and fault has a stable code from
  [`catalog/reason_codes.yaml`](catalog/reason_codes.yaml).
- **Nothing on the robot action path depends on the cloud.**

See [CONTRIBUTING.md](CONTRIBUTING.md) to work on Neura and [SECURITY.md](SECURITY.md) to report a
vulnerability. The engineering design, with the build plan and every decision, is
[docs/neura-design.md](docs/neura-design.md).

<div align="center">
<sub>Built by <b>NucleiLabs</b> · infrastructure that makes robot AI easier to run</sub>
</div>
