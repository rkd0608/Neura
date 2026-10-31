# Incident records

When a robot does something you didn't expect, the question is always *why did the runtime decide
that?* An incident record answers it: the runtime's last moments, exactly enough to replay them.

## What is recorded

`IncidentRecorder` (`neura_runtime.incident`) sits at the runtime's edges and keeps one ordered list:

- **inputs**: every control tick, chunk and rejection that reaches the runtime, session changes and
  operator commands, with the robot clock's time;
- **the robot's answers**: authority, each command's result, each fault response, and what the
  sensors returned (joint states and image sizes; image contents only when turned on);
- **decisions**: each action commanded, each chunk merged or refused and why, task epochs, faults,
  and each observation requested.

The list starts at a **checkpoint**, taken whenever a task epoch advances: that empties the queue,
the request sequence and any pending request, so a handful of facts is the runtime's whole state
there. A record covers about the last 30 seconds.

## When it is written

- **On a fault**, about a second later, so the hold that follows is in it.
- **On request**: the operator types `incident`.
- **On exit**: a record a fault is still waiting on is written before the process ends.

Files go to `incidents_dir` (default `incidents/`, next to the configuration), named
`<robot>-<UTC time>-<reason>.mcap`. Nothing is sent anywhere.

## Viewing

Open the file in [Foxglove](https://foxglove.dev) or [Rerun](https://rerun.io). Each kind of event
is its own JSON topic: `/neura/commanded` (actions sent, with values), `/neura/chunk` (accepted or
refused, and why), `/neura/fault`, `/neura/capture` (joint states), `/neura/input/*` (what reached
the runtime), and so on.

## Replaying

```bash
neura-robot replay incidents/so101-a-20261031T120000-STARVATION_GRACE_EXHAUSTED.mcap
```

The record carries its contracts and the replay data as attachments, so it replays anywhere. A
fresh runtime is restored to the record's checkpoint and fed its inputs, with the robot's recorded
answers; it must make exactly the same decisions, event for event, or the command names the first
event that differs. A record that replays the same is a regression test: keep it, and run it after
changing the runtime.

In Python:

```python
from neura_runtime import replay

outcome = replay.replay(replay.read(path))
assert outcome.matches, (outcome.first_difference, outcome.recorded, outcome.replayed)
```

## Limits

- A single task that runs past 500,000 events (about an hour) without its epoch advancing loses
  its start: the record keeps the newest events and is marked not replayable.
- Images are not recorded by default; replay gives the runtime blank frames of the right size,
  which its decisions don't depend on.
- The record covers the runtime, not the policy: why the model chose an action is the policy's to
  explain.
