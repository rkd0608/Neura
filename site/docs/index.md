---
title: What is Neura?
slug: /
sidebar_position: 1
description: Neura runs trained robot policies for many robots on a site's own GPU servers.
status: Experimental
---

# What is Neura?

Neura is robot AI serving infrastructure from NucleiLabs. It lets a robotics team deploy a trained
policy across several robots using shared GPU servers at their site, and manage that deployment as
a repeatable software operation: register a model, connect robots and servers, roll out, roll
back, and see what is happening.

## The problem

Teams that put learned policies on manipulation robots rebuild the same infrastructure for every
program: a process that runs the model next to each robot, the timing logic that keeps a robot fed
with actions, the handling of network hiccups and model crashes, and a way to ship a new model
without walking to every workcell. Usually each robot gets its own GPU, because sharing one safely
is hard.

Neura is that infrastructure, built once. The customer keeps their model, robot design, task
capability and operating procedures.

## Who it is for

Robot suppliers and integrators running several manipulation workcells, with GPU servers at the
site, who want to deploy policies they trained (or public ones such as ACT and SmolVLA) without
building and maintaining the serving layer themselves.

## How it is deployed

- **On the customer's own hardware.** The policy runs on GPU servers at the site, next to the
  robots, over the local network. Camera images, robot state and actions stay at the site.
- **Managed from the cloud, but never on the action path.** NucleiLabs' control plane holds the
  desired state: which model runs on which robots. The site keeps running if the cloud is
  unreachable, within limits the customer sets.
- **The robot decides what it executes.** A small runtime on each robot computer checks every
  action chunk before anything moves: it must be fresh, for the current task, and from the
  robot's own serving session. See [Architecture](./architecture.md).

## Where it stands

Neura is in early development. The platform described on these pages exists as software and is
exercised end to end with simulated robots; it has not yet been qualified for any model and robot
combination, and no part of it is a released product. [Development status](./status.md) lists
what exists, and [Compatibility](./compatibility.mdx) lists what has been qualified (nothing yet).
