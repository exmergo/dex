**A declared time column writes the Elementary monitors that watch it, behind a new `monitors.vendor` setting.**
With `monitors.vendor: elementary` in `.dex/config.yml`, a plan that renders a
declaration also writes `elementary.volume_anomalies` and
`elementary.freshness_anomalies` into the model's YAML entry, each with the
column declared `role: time` as its `timestamp_column`, in the same plan and
diff as the declaration. Several time columns give a freshness monitor on each
and no volume monitor; no time column gives neither. Dex chooses no training
setting: sensitivity, `days_back` and seasonality stay the package's defaults.

- A monitor is written only where the package is installed and the repository's
  `dbt_project.yml` enables it on the production target, read with `target.name`
  replaced by `dbt_prod_target`. Disabled, or not readable, and nothing is
  written; a warning says which.
- A monitor the project wrote by hand is reported `hand_written` and never
  touched, and one Dex wrote that the declaration no longer gives is reported
  `stale` and never removed.
- `data.monitors` reports the package and production reading and one row per
  monitor, and each monitor Dex wrote is a `data.decisions` entry naming the
  declaration field it came from, read back from the YAML. `transform declare`
  writes the monitors an earlier declaration owes.

Unset (the default), nothing changes. The rest of #1444's mapping rows build on
this setting.
