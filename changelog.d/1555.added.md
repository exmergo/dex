**Test failures fold into incidents by one rule, whatever their source.**
`exmergo_dex_core.maintain.incidents` adds an `Incident` value type and
`fold_incidents`, which turns the outcomes every test-result source records into
incidents: an observed failure opens or extends one, two consecutive passes from
the source that opened it close it (set per check), an unobserved run neither
opens nor closes one, a check that stopped running ends `no_longer_observed`
against a schedule derived from its history, and a source that disagrees is
attached as evidence. A first failure no source holds a pass before is reported
`unknown`. Incidents carry ids, statuses and times, never a value from the data.
The command that returns them comes in a later release.
