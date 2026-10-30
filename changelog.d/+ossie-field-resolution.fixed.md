**Ossie keys and relationships resolve through the fields they name.** Under
upstream's Name Resolution, `primary_key`, `unique_keys` and a relationship's
`from_columns` and `to_columns` name a dataset's **fields**, not its source
columns. dex read them as columns, so a field `customer_id` defined as
`o_custkey` sent `explore relationships --verify`, `explore profile
--use-project`, `maintain grain` and `maintain semantic` to a column that does not
exist, or to a different one of that name. Each member now reaches its column
through its field's expression; a key or relationship naming no declared field is
refused (`undeclared_key_field`, `undeclared_relationship_field`); and one with a
member whose field links no column (a computed field, for instance) is named in a
note and never verified or probed, so a composite is never checked on part of its
tuple. The snapshot fingerprints a relationship's fields as well as its columns,
so renaming either is drift.
