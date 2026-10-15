# Neura console

The web console (design §19): onboarding, sites and GPU servers, robots, models and compatibility,
execution profiles, capacity, releases with prepare, activation and rollback, live sessions,
faults, the audit log, and organization settings.

It uses only the public management API (`api/openapi.yaml`). Its client is generated from the spec
(`pnpm run gen`), so the console cannot call anything the CLI and SDK cannot. It shows what sites
observe, never what was requested. Every state has its reason in plain language, from the shared
reason-code catalog. Stale data looks stale. The console can request an activation but never
approve one: a person at the site approves it.

```bash
make console-setup          # pnpm install
make console-dev            # http://127.0.0.1:5173, proxied to the API on :8080 (make dev)
make console-check          # format, lint (incl. accessibility rules), strict types, unit tests
make console-e2e            # the whole journey in Chromium against the real backend
```

**Sign-in.** The console uses SSO when the control plane publishes an OIDC client in `/v1/meta`
(authorization code with PKCE, as a public client). API tokens are for development and automation.
Credentials live in `sessionStorage`, so closing the tab signs you out.

**Live updates.** The console follows the project's change events through `fetch`, because
`EventSource` cannot send an `Authorization` header. It resumes from the last cursor and refreshes
the cached queries that changed. Live views also poll as a fallback.

**End-to-end tests** (`e2e/`) need PostgreSQL. They default to `127.0.0.1:55432`, or set
`NEURA_E2E_DATABASE_URL` to an admin connection URL. `e2e/stack.py` creates a fresh database and
serves the control plane. The test then plays every role, running each real component:

1. The customer uses the console in the browser.
2. The GPU server runs the install command the console displays: a real site agent and gateway.
3. Simulated robots run the real robot runtime.
4. The site operator approves with `neura-agent approve`.

axe checks every main screen against WCAG 2.1 AA.

**Production image:** `docker build -f deploy/docker/Dockerfile --target neura-console .`. nginx
serves the build with strict security headers and proxies `/v1` to `NEURA_API_UPSTREAM` with
buffering off, so the event stream flows. Set `NEURA_CSP_CONNECT_EXTRA` to the OIDC issuer's
origin when SSO is used.
