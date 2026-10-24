# neura

The `neura` command, and `@neura.policy` for running any model on Neura.

```bash
pip install "neura[site]"                    # [site]: this machine can be a site's server

neura login                                  # sign in through the browser
neura site create lab --bench                # a bench: it approves its own activations
neura site join lab --simulate 1             # this machine runs the site's agent, one simulated arm
neura policy push ./checkpoint --name pick   # a LeRobot checkpoint, a @neura.policy file, or an image
neura try pick --on sim-1                    # checked, prepared at the site, switched
neura deploy pick --to arm-1,arm-2           # on production sites: shadow, then a person approves
neura approve                                # that person, at the site
neura status
neura rollback arm-1
```

Policies come from three sources, declared the same way:

- **LeRobot**: a checkpoint directory; its `config.json` says what it reads and returns.
- **Python**: a class with `@neura.policy(chunk_length=..., cameras=...)` and an `act(observation)`
  method. The file's directory (code and weights) ships with the version.
- **Container**: an image pinned by digest that answers Neura's policy interface, with a
  declaration file (`--declare policy.yaml`).

`neura check PATH --run` checks a policy against a robot and times it on this machine, without
pushing anything.

Versions are signed with this machine's publisher key (`~/.config/neura/publisher.key`), which
sites trust. Their files go to the sites this machine serves, or encrypted to Neura's registry
with `--seal-key`. Neura itself never sees them. Every command takes `--json`.
