# neura-contracts

The three versioned, immutable documents that bind a policy to a robot (design §4), and the
compatibility check between them.

| Document | Kind | Describes |
| --- | --- | --- |
| Model artifact manifest | `ModelArtifact` | Weights, runtime image, inputs, action outputs, properties, licenses |
| Robot profile | `RobotProfile` | Command interface, state features, cameras, controller, validated fault responses |
| Execution profile | `ExecutionProfile` | One artifact bound to one robot profile: timing, chunking, merge mode, freshness, starvation response, transforms |

Each document is identified by a content digest (`sha256:` over its RFC 8785 canonical JSON form),
so any other language can recompute it.

```python
from neura_contracts import check_compatibility, load_document

artifact = load_document(open("artifact.yaml").read())
robot = load_document(open("robot.yaml").read())
profile = load_document(open("profile.yaml").read())

report = check_compatibility(artifact, robot, profile)
for issue in report.issues:
    print(issue.code, issue.path, issue.message)
```

The check never converts anything implicitly. Units, frames, action order and semantics must match
exactly, unless the execution profile declares an explicit, supported transform.
