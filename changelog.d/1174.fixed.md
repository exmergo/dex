**Qualifying a GROUP BY key is no longer reported as a grain change.**
Table qualifiers on GROUP BY keys are stripped during comparison so alias-only
refactors do not trigger `group_by_changed` and keep the population classified
as kept. Real grain changes continue to be reported.
