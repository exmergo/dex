**Ship `cube.js` to enforce the `dex_scopes` claim on Dex's token.**
`scripts/cube/cube.js` defines `contextToApiScopes` restricting API endpoints to
the token's `dex_scopes` claim (`meta` and `sql`), and `scripts/setup_cube_dev.sh`
mounts it so `/v1/load` is refused to Dex's token.
