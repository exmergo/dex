**`dex init` no longer grows carriage returns in authored docs.** On Windows,
`dex init` wrote `AGENTS.md`, `CLAUDE.md` and `README.md` in text mode, so every
`\n` became `\r\n` and an authored line already ending in `\r\n` came out with
one more `\r` on each run, while the rewritten bytes never matched what dex
holds, so every run rewrote the files again. The write path now puts the text it
holds on disk verbatim, and a second run with nothing to change writes nothing.
