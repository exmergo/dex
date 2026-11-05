**YAML-mode relationship tests keep the version on `ref(x, v=N)`.**
without a manifest, a relationships test's `to: ref('dim_teams', v=2)` was
read as `dim_teams`, so declared joins disagreed with the compiled project.
the version label is kept, and a bare `ref()` to a versioned model resolves
to `latest_version` the same way the manifest reader does.
