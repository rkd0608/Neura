# Neura: customer value, simplicity and defensibility v0.1

Status: product hypotheses to validate in discovery and pilots · October 14, 2026
Companion to: `neura-design.md`

## 1. The value we deliver, in the customer's terms

A robot company running learned policies today rebuilds the same infrastructure for each program: serving, timing and buffering, fault handling, model release and rollback, monitoring, and capacity planning. Neura sells that infrastructure as a supported product.

| Value | What changes for the customer | How we measure it in a pilot |
| --- | --- | --- |
| **Engineering time not spent** | Their autonomy team stops building and maintaining serving and release tooling per robot program | Engineer-hours on deployment and infrastructure before vs during pilot; tickets and incidents owned |
| **Faster, safer model releases** | New policy versions go from "custom rollout" to staged, evidence-backed activation with rollback | Time from trained checkpoint to running on robots; failed or reverted releases |
| **Fewer stalls and incidents** | Starvation, stale actions and faults are handled consistently and are visible | Starvation events and deadline misses per robot-hour; intervention rate |
| **Lower compute per robot** (only where measured) | Several robots share a GPU when the workload allows it | GPU-hours per robot-hour vs the dedicated baseline (`neura-design.md` §10), including reserve capacity |
| **Evidence for their own customers** | Deployment, version and fault records they can show to end customers and auditors | Records produced; customer feedback |

Expected order of importance: engineering time and release safety are the most reliable value; compute savings depend on the workload and must never be the only claim. Some workloads will not benefit from sharing; we say so and still deliver the operational value, or we decline the deal.

Value equation for each pilot, agreed before it starts:

`value = avoided engineering cost + avoided incident cost + compute savings (measured) − Neura fees − integration effort`

## 2. Making it simple

Simplicity is a product requirement with acceptance tests, not a slogan.

| Step | Target experience |
| --- | --- |
| Connect a GPU server | One command, preflight report with fixes, no firewall changes (`neura-design.md` §17) |
| Bring a model | Point at an existing LeRobot-format checkpoint or a supported package; Neura builds the manifest and asks only about what it cannot infer |
| Check compatibility | Automatic contract check against the robot profile, with plain-language reasons for any mismatch |
| Know if it fits | Automatic profiling produces "this server can run N robots at your timing", with its assumptions shown |
| Integrate the robot | Open-source robot runtime and SDK; reference adapters for supported arms |
| Ship a new version | Stage, compare, activate at a task boundary, roll back: one workflow in console or CLI |
| Prove it yourself | Customers run the same conformance and fault suites we use |

Internal targets to validate (illustrative): first server connected in under 30 minutes; a supported policy running on a supported robot in simulation on day one; second installation needs no custom code from us.

## 3. Why customers would stay: defensibility, honestly

At launch Neura has **no moat**. The code is replicable, generic GPU hosting exists, model providers bundle serving, and strong teams can build in-house. Defensibility has to be earned, and these are the mechanisms that compound with each deployment:

1. **A qualified integration library.** Every policy × robot × controller combination we qualify (with adapters, conformance evidence and validated fault responses) makes the next customer with that combination cheap and fast. Competitors start from zero on each one.
2. **Embedded in the operating workflow.** Once releases, rollbacks, fault records and audit history run through Neura, replacing it means rebuilding those workflows and losing that history. We keep data exportable, so customers stay because the product is useful, not because they are locked in.
3. **Performance knowledge across hardware and models.** Profiles of latency, memory and capacity across GPU types, models and batch sizes make capacity estimates accurate out of the box. Only performance metadata is used, and only with customer consent; never payloads or weights.
4. **The open contract as an ecosystem.** If model developers and arm makers publish Neura-compatible packages and pass our conformance tests, Neura becomes the default way to deploy them. This is the route to standard-like adoption, and it only works if the contract is open (`neura-design.md` §14).
5. **A safety and reliability track record.** In robotics, a record of operating without incidents caused by the infrastructure, with an auditable open runtime, is slow to earn and hard to copy.
6. **Neutrality across model providers**, which matters to customers who use more than one model family. Not enough on its own, but it reinforces the points above.

Not moats: the scheduling algorithm, the transport, the dashboard. Those are table stakes.

## 4. Positioning against alternatives

| Alternative | When it wins | Where Neura must win |
| --- | --- | --- |
| Build in-house | Single program, strong infra team, unique hardware | Many programs or robot types; small teams; release safety; time to deployment |
| Generic GPU cloud | Hosting is the only need | On-site serving, robot-side semantics, fleet releases |
| Model-provider bundle | Customer uses only that provider's models | Mixed models, customer-owned weights, on-site data |
| Open-source alone (e.g. LeRobot remote inference) | Research, single robot | Multiple robots per server, fleet operations, support |

## 5. What to validate first

1. Do target customers rank infrastructure engineering time and release safety as a real problem with a budget?
2. Will they run a policy outside their present runtime?
3. Which measure would they sign a pilot against?
4. Does the second customer actually reuse the first customer's adapters?

If the answers are no, the strategy changes before more is built.
