# dex: drive the engine via its command contract

Follow [`AGENTS.md`](../AGENTS.md) at the repository root.

In short: dex's logic lives in the `exmergo-dex-core` engine. Drive it by running
`uv run python -m exmergo_dex_core <subcommand>`, read the single JSON envelope it
prints, and decide the next step. Never re-implement the engine in instructions.
The guardrails (read-only against data, writes confined to the repo as diffs,
dev-target-only builds, cost surfaced before spend, no credentials or raw rows in
context, PII flagged not surfaced) are enforced in the engine. See `AGENTS.md` for
the full command surface and envelope spec.
