**`dex maintain coverage` inventories the tests a dbt project declares.** It
reads a fresh `dbt parse` of a copy of the project, so it is free on every
connector, opens no connection and needs no baseline. For each enabled model,
source, snapshot and seed it counts its own generic, singular, unit and
freshness tests (with each test's severity), lists the tests on downstream
resources apart, says whether an incremental model has a unit test that takes
its `is_incremental()` branch, and splits documented columns into asserted and
unasserted. Every ratio in `data.summary` names its numerator, denominator and
what the denominator counts; package and disabled resources are counted and
named under `excluded`. It takes the `maintain verify` selector and `--full`,
reports only (never fails on low coverage), and is reviewed for values-free
mode. `DexEngine.coverage()` is the library spelling.
