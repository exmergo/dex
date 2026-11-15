**The skills stay on a supported Python when your newest is 3.15.** Dex supports
Python 3.11 to 3.14. The skill wrappers now hold both their own interpreter and
the engine's environment to that range, so uv picks an installed 3.14 or older,
or downloads one, instead of installing the engine on 3.15, where dependencies
that ship no 3.15 wheels fail to build (the Databricks extra did). An explicit
`UV_PYTHON` still wins. The README, the installation page and the package README
state the range, and the installation page shows `uv tool install --python 3.14`
for a standalone install on a machine whose newest Python is 3.15.
