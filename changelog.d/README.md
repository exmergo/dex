# Changelog fragments

Each pull request that changes something a user of dex can notice adds a file
here describing the change. Preparing a release collects the pending files into
[`CHANGELOG.md`](../CHANGELOG.md) and deletes them, so pull requests never edit
the changelog itself and never conflict there.

Name the file `<pull request number>.<type>.md`, with one of these types:

| Type | Use it for |
| --- | --- |
| `added` | A new command, flag, config key, connector or capability |
| `changed` | A change to existing behavior or output |
| `deprecated` | Something that still works and is going to be removed |
| `removed` | Something that no longer exists |
| `fixed` | A bug fix |
| `security` | A vulnerability fix or hardening |

For example, `changelog.d/613.changed.md`:

```markdown
**`--help` now describes every command and flag.** Each subcommand carries a
one-line summary in its group's listing and a description at the top of its
own `--help`. A test fails on any new subcommand or flag added without help.
```

The release appends the link to the pull request, so leave it out of the text.

- No pull request number yet: name the file `+<short-slug>.<type>.md`, for
  example `+help-text.changed.md`, and rename it once the pull request is open
  so the entry gets its link.
- Two kinds of change in one pull request: one file per type, such as
  `613.added.md` and `613.fixed.md`. Two entries of the same type:
  `613.fixed.md` and `613.fixed.1.md`.

Preview the release notes from the repository root:

```
python3 scripts/uvx_locked.py towncrier build --draft --version preview
```

The full instructions, including how to write a useful entry and when a pull
request needs no fragment, are in
[CONTRIBUTING.md](../CONTRIBUTING.md#changelog-fragments).
