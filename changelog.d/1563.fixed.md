**`fold_incidents` counts the passes that close an incident by run, and refuses
input it cannot order.** Two rows for one test in one run counted as two passes,
so an incident could close after a single passing run, and a run that both
failed and passed counted toward the close. Each source now has one outcome per
run, a failure over a pass, as the coverage timeline already merges a run.

A run id that carries two `sequence` values across the observations, the
sources' runs and the timelines is refused with a `FoldInputError`
(`run_sequence_conflict`), rather than read in an order that sets one source's
outcomes against another's wrongly. Where two sources fail in the run that opens
an incident, the source named first in `sources` opens it; a tie between sources
`sources` does not name is refused (`opener_tie_undeclared`). Before, the
alphabetically first source opened it, which could leave an incident that no
source could close.
