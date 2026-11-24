**`maintain incidents` no longer says no failure has a known start when
Elementary dates it.** When dbt's `run_results.json` is from an invocation
Elementary also recorded (`placed: in_window`), the incident's start comes from
Elementary's runs, and the run-results note saying the opposite is left out.
A run read alone, after Elementary's window, or with no place in it keeps the
note, since a failure seen only there still has no known start.
