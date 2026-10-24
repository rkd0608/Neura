# Security

Neura runs policies that move physical robots, at customer sites. We take reports seriously and
answer them quickly.

## Reporting a vulnerability

Please report privately, not in a public issue:

- through GitHub's **Report a vulnerability** (the repository's Security tab), or
- by email to **hello@nucleilabs.ai**, with "Security" in the subject.

Tell us what you found, how to reproduce it, and what it lets an attacker do. We acknowledge
within two working days, keep you informed while we fix it, and credit you if you wish.

## What matters most

The properties Neura is built to keep, and the ones we most want to hear about if they break:

- **No cloud credential, token or role can approve an activation or start motion.** Only a person
  at the site can.
- **A site runs only artifacts and agent releases signed by keys its operator trusts.**
- **Robots execute only fresh action chunks from their own session, for the current task.**
- **Camera frames, robot state, actions and weights stay at the site**; everything a site sends is
  in its hash-chained outbound ledger.
- **Workspaces are isolated**: one customer can never see or change another's data.

How sites are connected and secured, and the security review, are in
[docs/neura-design.md](docs/neura-design.md) (§17, and §17.8 for the review).

## Supported versions

Neura is in development and has no released versions yet. Fixes land on `main`.
