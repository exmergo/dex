module.exports = {
  contextToApiScopes: async (ctx, defaults) => ctx.securityContext.dex_scopes ?? defaults,
};
