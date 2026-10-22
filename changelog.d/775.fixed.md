**Semantic YAML is validated against MetricFlow's schemas, and the command that
authors it installs them.** `semantic define|update|plan` checked the layer
structurally only, in every supported install: the schema package was imported
under a name the installed MetricFlow does not ship, and the extra that
provides it was never taken by the command that needs it. The first validation
layer the semantic-layer reference documents therefore never ran, and the
warning that said so read like routine news.

The schemas now load under either package name, the authoring commands take the
`semantic` extra, and where MetricFlow is absent the warning names the missing
package and the extra that provides it. Validation that never ran will now run,
so YAML that planned before may be refused; the refusal is the schema's own
message.
