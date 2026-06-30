## What this changes

<!-- A short description of the change and why it is needed. -->

## Related issues

<!-- e.g. Closes #123. Delete if none. -->

## Checklist

- [ ] Engine tests pass (`uv run pytest` in `packages/dex-core`)
- [ ] Eval scoring-core tests pass (`uvx pytest evals`)
- [ ] If a safety path is touched, the spine still holds: read-only against data,
      cost-guarded, PII flagged not surfaced, propose-don't-impose
- [ ] `CHANGELOG.md` updated under `[Unreleased]`
- [ ] Prose is em-dash free (checked in CI)
- [ ] No credentials, secrets, or raw warehouse rows in code, tests, or fixtures
