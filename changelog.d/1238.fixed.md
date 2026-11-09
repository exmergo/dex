**`--verify` reads layer schemas scaffolded by `transform init --layered-schemas`.**
the verification sweep and dev-target reads derive `<layer>_<target name>`
schemas alongside `<dev schema>_<name>`, so warehouse models built across
staging, intermediate and mart schemas are judged instead of suppressed.
