# Security policy

neura-robot runs on robot computers and decides what a robot executes. We treat two kinds of
problem as security issues:

- **Safety-rule bypasses**: any way for an action to reach the controller that the runtime should
  have refused (stale, from a finished task, out of range, non-finite, unarmed, after a takeover
  or a latched fault).
- **Classic vulnerabilities**: code execution, authentication bypass on `neura-robot serve`,
  unsafe deserialization (for example, in the LeRobot transport's reply decoding), path traversal.

## Reporting

**Please do not open a public issue.** Report privately through GitHub:
[Security → Report a vulnerability](https://github.com/rkd0608/neura-robot/security/advisories/new).

Include what you can: the version or commit, the configuration, steps to reproduce, and an
incident record (`.mcap`) if the robot misbehaved. We will acknowledge within 3 working days,
keep you informed, and credit you in the advisory unless you prefer otherwise.

## Supported versions

Only the latest release, and `main`, receive fixes while the project is in alpha.

## Hardening notes

- `neura-robot serve` refuses to listen beyond loopback without a token (`NEURA_SERVE_TOKEN`).
  Its traffic is **not encrypted**: on an untrusted network, put it behind TLS (a reverse proxy or
  a VPN) until built-in TLS lands.
- The LeRobot transport decodes LeRobot's pickled replies with a restricted unpickler and
  weights-only tensor loading, but the server is still trusted with the robot's observations and
  its actions. Connect robots only to policy servers you run.
- neura-robot is not a safety-rated controller. Keep the robot's own e-stop and safety functions
  in place; neura-robot works inside them.
