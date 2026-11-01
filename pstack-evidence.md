# pstack evidence — exmergo/dex#1030

## Goal
Refuse `population.parent` that names nothing on `semantic ossie|slayer|cube`, matching the dbt-route check; keep valid layer parents planning.

## Repro (before)
On main @ 9105edd, Ossie fixture from `tests/ossie/test_derivation.py` (ORDERS_TEXT + COMMERCE):
`semantic_native("ossie","plan",..., declarations=[{model:orders, population.parent: no_such_model / not_a_column}])`
→ ACCEPTED, plan stored with `ai_context.dex.population.parent.model: no_such_model`.

## Mechanism
`transform/native_semantic._annotate` only ran `parse_declarations` (shape) then vendor `derive_semantic` annotate, which carries population as metadata without resolving parent. dbt route uses `_check_parent` in `transform/declarations.py`.

## Fix (smallest)
1. Shared `transform/native_population_parent.refuse_population_parents` on the native route.
2. Vendor-neutral inventory via duck-typed `layer.declaration_models(documents)` (Ossie/SLayer/Cube).
3. Call from `_annotate` after documents overlay, before `derive_semantic`.
4. dbt fallback via lazy `find_project`/`load` when parent not in layer inventory.

## Verify
- Ossie refuse/accept engine repro + pytest (2 new) pass.
- Cube refuse pytest pass.
- `tests/transform/test_native_semantic_route.py` 10 passed.
- Slayer test added; module skipped locally (no [slayer] extra).
