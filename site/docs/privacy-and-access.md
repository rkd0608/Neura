---
title: Privacy and access
sidebar_position: 4
description: What stays at the site, what leaves it and how to check, customer-managed keys, and who may do what.
status: Experimental
---

# Privacy and access

## Where data lives

| Class | Examples | Where it lives | Leaves the site? |
| --- | --- | --- | --- |
| Payload | Camera images, robot state, actions, language instructions | Site memory only | No: it is in no message the site sends |
| Model | Weights, normalization statistics | The site's artifact store; optionally a registry | Only if the customer chooses a registry, then sealed with their key |
| Operational metadata | Digests, identifiers, states, reason codes | Site store | Yes, under a published schema |
| Aggregate metrics | Latency percentiles, deadline misses | Site | Optional: `minimal` telemetry keeps them at the site |

## What leaves the site, and how to check

The site agent sends only the fields in the [published outbound schema](./reference/outbound.md)
and drops anything else before it leaves the server. In `minimal` telemetry mode it sends only
health, versions, identifiers, states and reason codes: no free text and no metrics.

Every message it sends is recorded in the site's **outbound ledger**, a hash-chained log on the
server. The customer can summarize it and verify the chain instead of trusting this page:

```bash
neura-agent outbound --summary
neura-agent outbound --verify
```

Metrics for the customer's own monitoring are on every component's Prometheus endpoint, with
Grafana dashboards shipped in the repository.

## Customer-managed keys

Model weights can be **sealed** with a key the customer holds (AES-256-GCM, with the data key
wrapped by a key file or the customer's KMS through a command). Only a site with the key can open
them. Sealed weights can be kept in any store, including a registry run by NucleiLabs, which only
ever holds ciphertext. Revoking the key revokes access.

## Who may run what

- **Publisher signatures.** A site prepares an artifact only if a publisher key its operator
  trusts signed it.
- **Signed desired state.** The control plane signs what it asks a site to run; the agent verifies
  it, and the contracts in it, before acting.
- **Mutual TLS** between robots and the gateway (certificates from the site's own CA) and between
  the agent and the control plane.
- **Sandboxed workers.** Policy workers run the artifact's pinned image with no network, a
  read-only root filesystem and no capabilities.

## Roles

| Role | Can | Cannot |
| --- | --- | --- |
| Viewer | See status, metrics and audit | Change anything |
| Deployer | Register models and profiles, prepare revisions, request rollout or rollback | Approve physical activation |
| Site operator | Approve or refuse activation at the site, take over, reset faults | Change organization settings |
| Admin | Manage the organization, projects, sites, sign-in, tokens and support access | Approve motion, unless also a site operator |

Deploying and approving motion are always separate: the cloud can request an activation, but a
person at the site approves it, on the server, with a local operator credential.
