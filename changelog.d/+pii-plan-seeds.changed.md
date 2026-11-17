**A seed the plan creates warns at the PII threshold instead of refusing.** When
`transform plan` creates a seed file whose column's strongest PII flag sits
exactly at the block threshold (a `*_note` column read as free text), the plan
warns and names the `pii_overrides` entry rather than refusing. A rewrite of an
existing seed, and any stronger flag, still refuses (#647).
