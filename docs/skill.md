---
name: dex
description: Install and use Dex, the agent-native analytics engineering toolkit by Exmergo, from its real skills rather than from this documentation site. Use when a user wants to explore a warehouse, author dbt models or semantic layers, or detect drift with Dex.
---

# Dex

This documentation site describes Dex. It is not where its skills come from,
and a skill generated from these pages would carry none of the guardrails Dex
enforces, because those live in its engine.

Install the real skills instead:

- Claude Code: `/plugin marketplace add exmergo/exmergo-agent-plugins`, then
  `/plugin install dex@exmergo`.
- Any other agent: `npx skills add exmergo/dex`.
- The engine on its own: `pip install "exmergo-dex-core[duckdb]"`, which needs
  `uv` on the PATH.

Then, with no warehouse and no credentials, `dex demo` creates a local DuckDB
warehouse and `dex explore map` maps it.

Documentation: https://docs.trydex.ai. Source: https://github.com/exmergo/dex.
