**Cube Cloud, and Cube executing a query with `--api`.** `semantic.deployment:
cube_cloud` reads a Cube Cloud deployment of the repository's Cube model files:
by default Cube Cloud executes the query over `/v1/load`, and the catalog is the
deployment's compiled `/v1/meta`, which answers JavaScript, Jinja and `extends`
definitions, with every result saying it was not checked against the files.
`--local` has Cube Cloud render the SQL for Dex to execute under the cost guard,
held to the reviewed files. On a Cube Core, `--api` now has the Core execute
instead of being refused. Wherever Cube executes, the PII gate runs before
anything is sent, results report `execution: vendor` with a cost-guard
unavailable warning, and relayed warehouse errors are masked. The Cube Cloud
token is `CUBE_API_TOKEN`, sent only to a Cube Cloud host or one named in
`DEX_CUBE_ALLOWED_HOSTS`, and `DEX_CUBE_URL` overrides `semantic.cube.url`.
`scripts/setup_cube_dev.sh --execute` starts an executing Cube Core, and the
integration workflow gains `--api` cases and a narrow Cube Cloud job (#914, #915).
