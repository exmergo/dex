**A flow-style `config`, `meta` or entry no longer blocks a declaration.**
`transform plan` and `transform declare` refused to write a declaration whose
entry, `config` or `config.meta` mapping was written in flow style
(`config: {meta: {dex: {grain: [event_id]}}}`), quoting the style instead of
recording the decision; in live agent runs, three of three agents stopped
there and the assumption went unrecorded. Dex now converts just that mapping
to block style in the same `schema.yml` edit, keeping its keys and values and
every byte outside it, so the plan writes the declaration and the diff shows
the restyle. A mapping that an alias references (used more than once) still
refuses with the reason named, and an entry nested in a flow-style `models:`
list still does, because a block entry cannot sit inside one.
