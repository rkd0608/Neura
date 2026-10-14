# Example contracts

Illustrative documents for an SO-101-class arm served with ACT and SmolVLA. They exercise the
contract format and the compatibility check; they are **not** qualified configurations (design
§2.3), and the weight and image digests are placeholders.

```bash
uv run neura-contracts check \
  --artifact examples/contracts/act-so101.artifact.yaml \
  --robot examples/contracts/so101.robot.yaml \
  --profile examples/contracts/act-so101-30hz.profile.yaml
```

Execution profiles bind to the exact digests of an artifact and a robot profile. After editing
either, run `make examples` to refresh the bindings.
