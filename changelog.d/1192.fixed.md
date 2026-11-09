**The toggle check judges what dbt compiled, not the source it could not
parse (#1156).** A select list and its `group by` count gated with
`{% if var(...) %}` left text the reference scan could not parse once its Jinja
was blanked, and `data.toggle` came back `unchecked` with "could not parse the
SQL for columns" on exactly the files the check exists for. Those limits are
gone, since every model is already judged from its compiled SQL at both values.

- A flag read in a project macro is attributed to the models whose compiled SQL calls it (`macro_reads`), and a `var()` with no literal name is listed under `dynamic_reads` with its model, rather than limiting the verdict.
- A disabled model that reads the flag is named under `not_compiled`, and the verdict is incomplete for it alone.
- dbt stops a compile at the first model that fails and then writes no compiled SQL for any model. Dex now sets that model aside (`compile_failed` on its own record) and compiles the rest again, so one broken model no longer leaves the whole project unjudged.
- `transform plan` accepts a Jinja edit to a disabled model. It used to refuse with "dbt returned no fresh compiled SQL". The model is now compiled enabled in the throwaway copy, so its SQL is still held to the read-only checks, and a warning says it is not built.
