# ADE-bench (home benchmark)

ADE-bench (dbt Labs, Apache-2.0) is dex's home benchmark. It runs
Claude Code with plugin-sets and a real `--plugin-set none` baseline, supports
`--agent claude / codex / gemini`, grades by dbt tests plus table-equality against
answer-key seeds, runs on DuckDB or Snowflake, and reports cost, tokens, and
turns. There is no official leaderboard, so dex publishes its own attributed
numbers. Semantic-model maintenance is a confirmed gap dex contributes into.

## Phase 0 spike (run locally, with your environment)

Confirm the runner works before depending on it:

```bash
# In a checkout of ADE-bench:
ade-bench run --db duckdb --plugin-set none
```

## Phase 5 (what lands here)

- A dex plugin-set config (skills + allowed-tools) and a runner.
- Runs vs `none` and vs dbt Labs' own dbt-skills set.
- Published uplift plus cost and turn efficiency, attributed.
- A first multi-agent run via `AGENTS.md` on Codex or Gemini.
