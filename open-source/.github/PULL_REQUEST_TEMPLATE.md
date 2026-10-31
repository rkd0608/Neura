## What changes, and why

<!-- One or two sentences. Link the issue. -->

## How it is proven

<!-- The test that fails without this change; what you ran (`make check`, `make test-docker`, on
     hardware?), and anything you could not test. -->

## Checklist

- [ ] `make check` passes
- [ ] A bug fix comes with the test that fails without it
- [ ] No safety rule is relaxed (invariant monitor at zero violations)
- [ ] Docs and `CHANGELOG.md` updated, if people can see the change
- [ ] Commits signed off (`git commit -s`)
