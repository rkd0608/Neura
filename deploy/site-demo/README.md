# The site kit: be the customer

This kit runs a customer's site on your machine and connects it to your Neura:
- a site server: the site agent, its gateway and the policy workers;
- two robots;
- a page to watch the robots.

The robots are simulated ALOHA arms in MuJoCo, running LeRobot's published
`act_aloha_sim_transfer_cube_human` policy. They hand a cube from one arm to the other, task after
task.

Everything on the site runs Neura's real code, the same as on a real site:
- the agent enrolls with your Neura (outbound only) and checks the publisher's signature on the
  model;
- the gateway serves the model to the robots over mutual TLS;
- each robot runs Neura's robot runtime;
- revisions switch only between tasks;
- every activation waits for the site operator's approval.

You drive it all from the Neura console.

Needs Docker with Compose v2 (Docker Desktop on a Mac or Windows, or Docker Engine on Linux),
about 6 GB of disk and 4 GB of memory. No GPU is needed: the model runs on the CPU.

## 1. Publish the model (the ML team)

In the console, open your project and copy its URL from the browser. Then:

```sh
git clone https://github.com/rkd0608/Neura.git && cd Neura/deploy/site-demo
./site.sh publish 'https://console.<your-domain>/o/<org>/p/<project>'
```

It asks for an API token (create one in the console, under Settings, then API tokens). The
token is not stored. Then it:
- builds the kit's image (a few minutes the first time);
- downloads the checkpoint from the Hugging Face Hub;
- writes its three contracts: the robot profile, the model artifact (every weight file by
  digest) and the execution profile;
- signs the artifact with a publisher key it makes for you, kept in the kit;
- registers the contracts in your project.

## 2. Add the site and its robots (the console)

1. **Sites**: create a site, say "My lab".
2. **Robots**: add two robots at that site, with the robot profile `aloha-sim-transfer-cube`.
   Name them as you like (`aloha-1`, `aloha-2`).
3. The site's page, **Add a server**: copy the install command.

## 3. Connect the site (the site's operator)

```sh
./site.sh connect 'neura-agent enroll --control-plane enroll.<your-domain>:443 --token nen_...'
```

This does four things:
- starts the site agent and enrolls it (the token works once and expires);
- trusts your publisher's key, so the server runs only artifacts it signed;
- imports the model's weights into the site's store, each file checked against the artifact;
- starts the robot cell.

The console shows the server connected. Open **http://127.0.0.1:8095**: the two robots are
there, connected to the gateway, waiting for a revision.

## 4. Ship the model to the robots (the console)

1. **Deployments**: create a deployment for the site's robots, then add a revision with the
   artifact `act-aloha-transfer-cube` and its execution profile.
2. **Prepare** the revision. The site checks the contracts and the signature, loads the model
   and measures it, then reports how many robots this machine can serve at this profile.

   A recent laptop serves two or more: on a CPU, a forward pass takes 100 to 150 ms. A slower
   machine may serve fewer; a 4-core cloud VM takes about 550 ms and serves one. An activation
   for more robots than that fails at once with `ADMISSION_CAPACITY_EXCEEDED`. Then either:
   - remove a robot from the deployment;
   - or give each request more time: `./site.sh publish <url> --refill-at 65`, then add a
     revision with the new execution profile.
3. **Activate** it. On the cell's page an approval appears: approve it as the site operator (or
   run `./site.sh approve`). The cloud can ask, but only the site can say yes.

The robots start: each task, the cube is somewhere new. Watch the robots on the page:
- each robot's camera: the room view, or what the policy sees;
- its tasks and successes;
- the chunks served and the round trip.

Watch the same robots in the console: sessions, faults and activity.

## Things to try

- **Ship another revision while they work.** Publish with different settings, for example
  `./site.sh publish <url> --refill-at 40`. Add a revision with the new execution profile, then
  prepare and activate it. Each robot finishes its current task on the old revision and
  switches before the next.
- **Take over a robot** from its page. The policy loses authority at once and the arm holds.
- **Cut the site off.** Stop Neura, or your network, and the robots keep working: the site runs
  on its signed configuration until its offline authorization runs out.
- **What left the site**: `./site.sh status`. Run `docker compose exec site-agent neura-agent
  outbound --summary` for every message the server sent to Neura, in a hash-chained ledger.

## Commands

| | |
|---|---|
| `./site.sh publish URL [--checkpoint REPO\|DIR] [--refill-at N]` | register a model's contracts |
| `./site.sh connect 'INSTALL COMMAND'` | enroll this site's server; start the robots |
| `./site.sh approve` | approve what waits for the site operator |
| `./site.sh status` | the site agent's view |
| `./site.sh logs [site-agent\|cell]` | follow the logs |
| `./site.sh down` | stop (the site's state is kept) |
| `./site.sh forget` | stop and delete the site's state |

## Different from a production site

The kit sets `NEURA_AGENT_DEVELOPMENT`, which allows two shortcuts:
- **Robot certificates.** The agent issues the simulated robots their keys and certificates.
  Real robots make their own key and send a certificate request (`neura-robot keygen`, then
  `neura-agent robot-certificate`).
- **Policy workers.** The workers run as processes in the agent's container. On a production
  site they run in the artifact's pinned runtime image, with no network, a read-only root and
  no capabilities.

The execution profile is also tuned for CPU serving:
- an observation may be up to 600 ms old when it reaches the model (100 ms on a GPU);
- the next chunk is requested with 50 actions left, a one-second budget.

On a GPU, publish with `--dispatch-freshness-ms 100 --refill-at 30`.

Its operator credential is in `.env`, readable by you only. The cell's page has no sign-in and
listens on `127.0.0.1` only.

To try the kit against a local development stack (`make dev`) instead of a cloud Neura, run
`tools/smoke_site_kit.py`.
