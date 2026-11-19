// The configuration of an *executing* Cube Core (scripts/setup_cube_dev.sh
// --execute), the server `explore semantic --api` asks to run a query (#914).
//
// Scopes as in cube.js: dex's token names the endpoints it may call, so a render
// token cannot load and a load token cannot render.
//
// For DuckDB the warehouse is in-memory and built from scripts/cube/shop.sql, the
// same rows dex's own DuckDB fixture holds, so a number this server executes and
// one dex executes come from one table. Every other database type takes Cube's
// own CUBEJS_DB_* settings, which setup_cube_dev.sh passes per type.
const fs = require("fs");

const shop = "/cube/conf/shop.sql";

module.exports = {
  contextToApiScopes: async (ctx, defaults) => ctx.dex_scopes ?? defaults,
  ...(process.env.CUBEJS_DB_TYPE === "duckdb"
    ? {
        driverFactory: () => ({
          type: "duckdb",
          initSql: fs.readFileSync(shop, "utf8"),
        }),
      }
    : {}),
};
