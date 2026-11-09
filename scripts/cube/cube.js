module.exports = {
  contextToApiScopes: async (ctx, defaults) => ctx.dex_scopes ?? defaults,
};
