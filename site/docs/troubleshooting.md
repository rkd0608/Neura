---
title: Troubleshooting
sidebar_position: 10
description: What the most common reason codes mean and what to do about them.
status: Experimental
---

# Troubleshooting

Every refusal and fault carries a stable [reason code](./reference/reason-codes.md). These are the
ones met most often while running the quickstart, the local stack and the chaos tests.

## A robot never starts moving

| Reason | What it means | What to do |
| --- | --- | --- |
| `LEASE_BUSY` | Another session holds this robot's lease: a second process with the same identity, or the previous session's lease has not expired yet. | Stop the other process. A lease expires a few seconds after its holder stops renewing it. |
| `ADMISSION_CAPACITY_EXCEEDED` | Serving one more robot would make the robots already admitted miss their deadlines. | Serve fewer robots per GPU, or check the capacity estimate the site computed at prepare. After a run of stalls, admission recovers by itself within two minutes once they stop. |
| `DEPLOYMENT_NOT_ACTIVE` | No active deployment includes this robot with this revision and these contract digests. | Check the robot's assignment and that the revision was activated at the site. |
| `AUTH_DEVICE_UNKNOWN`, `AUTH_DEVICE_REVOKED` | The robot's certificate (or development token) is not one the gateway knows, or it was revoked. | Issue the robot a new certificate from the site. |

## A robot stops and faults

| Reason | What it means | What to do |
| --- | --- | --- |
| `STARVATION_GRACE_EXHAUSTED` | No fresh actions arrived within the starvation grace after the queue ran out: network outage, gateway or worker restart, or an overloaded GPU. | Find the cause in the gateway's status and metrics, then reset the fault locally. |
| `ACTION_OUT_OF_RANGE` | The model produced an action outside the robot's declared command ranges. Nothing out of range was sent to the controller. | Check the model, its normalization and the robot profile's ranges. |
| `CONTROLLER_COMMAND_FAILED` | The controller refused a command. The runtime stopped sending commands and invoked the robot's fault response: the customer's independent stop path if the robot profile declares one. | Check the controller. |
| `ACTUATOR_LOCK_LOST` | The runtime no longer holds exclusive control of the actuator. | Check for another process using the controller. |

A fault is latched: reconnecting, a late reply or a cloud command never clears it. A site operator
resets it locally after checking the robot.

## A revision does not prepare

| Reason | What it means | What to do |
| --- | --- | --- |
| `AGENT_ARTIFACT_UNTRUSTED` | No publisher key the site trusts signed this artifact. | Sign it with your release key and trust that key at the site (`neura-agent trust-publisher`). |
| `AGENT_ARTIFACT_SIGNATURE_INVALID` | A signature does not verify against the artifact's digest. | Sign the registered artifact again. |
| `AGENT_ARTIFACT_KEY_UNAVAILABLE` | The weights are sealed and the site does not hold the key that opens them. | Configure the key file or KMS command at the site. |
| `WORKER_IMAGE_UNAVAILABLE` | The artifact's runtime image could not be pulled by digest. | Make the image reachable from the server, or pull it there first. |
