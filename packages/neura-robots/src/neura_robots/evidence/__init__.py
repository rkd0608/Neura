"""Evidence: published policies, on the scenes they were trained for, served by Neura and by the
alternatives a robotics team would otherwise use, with the same seeds and the same faults.

- ``policy``: a LeRobot checkpoint loaded and run exactly as ``lerobot-eval`` runs it.
- ``local``: the in-process baselines. ``lerobot-eval``'s protocol (the world waits while the
  policy computes: the number LeRobot publishes) and the real-time one (the world keeps moving
  while the policy computes, as on a robot computer running ``lerobot-record --policy.path``).
- ``served``: the full Neura path. The simulated ALOHA (``aloha.arm``) behind the real robot
  runtime and ``neura-arm``'s process, a site gateway with real policy worker processes, gRPC
  over TCP through the fault proxy.
- ``cli``: ``neura-evidence``.

Everything here measures task success on trained weights, or says that it does not.
"""
