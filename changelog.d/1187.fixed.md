**`explore query --target dev` reads a model `transform build` just wrote.**
On a warehouse, a model whose only build is the dev one could be profiled with
`explore profile --target dev`, but `explore query` had no `--target`. The
aggregate that checks the build was refused as "not in the exploration cache",
naming the `explore profile` the caller had already run. `explore query` now
takes the same flag: for one command it reads the dev namespace and the
namespaces dbt derives from it for the project's models, such as a package's
`+schema`. The firewall, the PII gate, values-free mode and the cost handshake
are unchanged, `--scope` still only narrows, nothing is written to
`.dex/config.yml`, and a prod-looking target is refused. Without the flag, the
refusal names the dev namespace and `--target dev`. The same flag on
`explore profile` also reads the derived namespaces now, so a package's model can be
profiled. If the connection cannot be opened (for example, a DuckDB file that
another process holds locked), a refusal for an uncached relation says why,
instead of pointing at `explore map`, which needs the same connection.
