**`maintain reconcile` no longer stages a YAML edit for a source rename the staging model already absorbs.**
when a model keeps its contract with an alias such as `page as page_path` over a
renamed source column, the SQL splice is correctly a no-op, but the YAML half
used to follow the source shape anyway and would break the model's column
contract. YAML is staged only when the model's output actually changes; absorbed
drift is reported as covered.
