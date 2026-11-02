**`transform build` says who owns a failed prerequisite, and `--cold-deps` proves a
repair survives a reinstall.** `data.prerequisites` names each failed node's owner
(project or package), its relation to the named plan, the required models behind
it, and the repair routes dbt supports, including why an override does not apply
(a reader pinned with `ref('pkg', 'model')`, a versioned or Python model).
`--cold-deps` reinstalls packages into an isolated copy and builds from it, under
the usual dev-only and cost gates. `data.cold_verification` lists the pins and every
file the installed tree held that the reinstall did not reproduce, so an edit under
`dbt_packages/` shows up as lost. An ordinary build never reinstalls. The recipe is in
`skills/transform/references/prerequisite-repairs.md`.
