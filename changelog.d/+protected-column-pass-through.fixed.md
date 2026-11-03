**Agents preserve authorized protected columns when inspection is refused.** The
shared procedure distinguishes a protected-value read refusal from a supported
transformation: retain the requested or existing column contract, validate with
metadata and permitted aggregates, and redact only when an applicable instruction
or contract requires it. Privacy guards and human-only PII clearing are unchanged.
