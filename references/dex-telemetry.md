# Dex Telemetry: open-source Datadog for analytics

> *When you think you know something, that is a most perfect barrier against
> learning.*
>
> Frank Herbert, *God Emperor of Dune*

You set up analytics to monitor your business. But who's monitoring your
analytics?

A pipeline can run on time, pass its tests and still deliver the wrong number:
a batch arrives twice, a record arrives late, a source stops sending one
category. Nothing errors. The report is simply wrong, and the first person to
notice is the one who acts on it.

Dex Telemetry watches the pipeline your reports depend on, the way Datadog
watches your app. Every dbt run is recorded, the rules you declare on your
models become checks on every run, failures become incidents, and your coding
agent is sent to find the cause. Any fix comes back as a plan you review. It is
built on dbt and [Elementary](https://github.com/elementary-data/elementary),
and it is part of Dex: open source under Apache 2.0.

**Telemetry here means monitoring your pipelines, not reporting on you.** The
run history, rules and incidents live in your warehouse, your repository and
`.dex/`. Dex sends nothing to Exmergo: no usage reporting, no license check, no
update ping.

## The imperial records

An illustrated workflow on synthetic data. The figures are invented and
checked, not taken from a real run.

The Emperor's pipeline has run for 300 nights, and Dex has recorded every one:
every run, every rule, every arrival time, in his repository where his agents
can read it. Tonight a storm grounds the *Ithaca* ship on Arrakis. At 04:00 the
pipeline runs without it. The run succeeds, nothing looks wrong, and the report
would tell him at dawn that spice shipments fell 40%.

At 04:01 Dex opens an incident: his rule says every ship booked tonight appears
in the report, even if it's late, and *Ithaca*'s 40 t are missing. While he
sleeps, his agent picks up the incident, finds the cause and writes a fix: wait
for late records, then rebuild. At 06:31 he reads the fix and approves it, and
the report says 100 t. Without Dex, he reads the 40% drop and fires the crew of
a ship that delivered.

The story is told as a 70-second film,
[The imperial records](https://www.youtube.com/watch?v=RBcf9eyZlaU). The
story's cause is one `maintain diagnose` confirms: rows of a source that landed
after the run read them, read from each row's arrival time against the run's
start. `dex demo` seeds it, beside a key the loader delivered twice, so you can
run both yourself (see [Try it offline](#try-it-offline)).

## How it works

| Step | What happens | Command |
|---|---|---|
| Install | Dex sets up the run history | `dex init` |
| Set rules | Write down what must be true | `transform plan` |
| Watch | Every run is checked and every result kept | `maintain incidents` |
| Investigate | Your agent finds the cause | `maintain diagnose` |
| Approve | Nothing changes until you say yes | `transform apply` |

### 1. Install

`dex init` offers the Elementary dbt package on every install, so every
production run uploads its test results to your warehouse. The package's models
are disabled on the dev target, so a dev build runs none of its work, and its
sampling of failing rows is off. Init also proposes the schema the package
writes in production for the source allowlist, as a `dex config set` you
confirm. `--no-elementary` leaves the package out.

### 2. Set rules

The rules are your models' declarations, the same ones
[Dex Semantics](dex-semantics.md) records: what one row is, which rows belong,
how each number adds up, and when the data must be fresh. They become checks
that run where your pipeline runs:

- **dbt tests.** A grain renders `unique` and `not_null`, a foreign key a
  `relationships` test, and with `declarations.render_tests` on, Dex's
  generated tests check the population, units, freshness and continuity.
- **Elementary monitors.** With `monitors.vendor: elementary` in
  `.dex/config.yml`, a declared time column becomes volume and freshness
  anomaly monitors, a dimension with a small domain a dimension anomaly
  monitor, a unit or a measure a column anomaly monitor, and a declared daily
  deadline a `data_freshness_sla`. A declared population filter becomes each
  monitor's `where_expression`, so it measures only the rows the model
  promises.
- **Personal data.** Columns flagged as PII are tagged so Elementary keeps no
  sample of their values.

Every test and monitor is written into the model's YAML as a reviewable diff.
Dex chooses no training setting for an anomaly monitor; those stay the
package's defaults.

### 3. Watch

`dex maintain incidents` reads the history Elementary uploaded, and dbt's
`run_results.json`, and folds both through one lifecycle rule:

- an observed failure opens an incident or extends the open one;
- two consecutive observed passes close it, so a test that flaps stays one
  incident;
- a run that recorded nothing is never read as a pass, and a check that stops
  running ends as `no_longer_observed`, never as closed.

The read is a run count (the newest 20 invocations by default, up to 2000), and
on a billed warehouse it is priced before it runs. `dex maintain verify` also
checks each declared freshness on demand: a `max_lag` against the warehouse's
clock, and a daily `deadline` in its time zone.

### 4. Investigate

`dex maintain diagnose` takes each open incident to a cause. It places the
onset between the last observed-good run and the first observed-bad one, with
the commits in between, bounds it with the committed `maintain snapshot`
series, and names every window no recorded state covers. Each candidate cause
is answered by a check Dex already has: rows a source delivered after the run
read them, a key the loader delivered twice, a source column the loader
retyped, a model the project changed in the range, a package upgraded, a seed
edited, or a parent model the schedule built after its child. The verdict is a cause, several causes, or none, with what was ruled
out. It never names a likeliest guess.

Each diagnosis also names the semantic models and metrics that read the broken
column, so you know which reports to hold.

### 5. Approve

A confirmed cause's repair is handed to `transform`, which returns it as a plan
of reviewable diffs. Nothing in your project changes until you run
`transform apply`, and the build that follows is checked again.

## Datadog, for your analytics

| Datadog, for your app | Dex Telemetry, for your analytics |
|---|---|
| Install the agent on every host | `dex init` sets up the run history |
| Metrics and heartbeat history | Every run recorded, in your warehouse and repository |
| Monitors | Your declarations become tests and monitors |
| Incident | Dex opens an incident; your agent is sent to it |
| Root cause | `maintain diagnose` traces it to its cause |
| (no equivalent) | The fix, as a plan you approve |

## Try it offline

`dex demo` writes a local DuckDB warehouse, a small dbt project and the
history of seven weekly builds in Elementary's tables. It holds two incidents.
In the last two builds, a uniqueness test fails because a batch was loaded
twice. In the newest build, a freshness test fails because the last twelve hours'
shipments landed minutes after the build started. No warehouse, no
credentials, no network:

```bash
uv tool install "exmergo-dex-core[duckdb]"
dex demo
dex maintain incidents   # two open incidents, each with its first failing build
dex maintain diagnose    # the causes: a key delivered twice, shipments that landed late
```

## Further reading

- [Test failures as incidents](incidents.md): the lifecycle rule, the coverage
  timeline, what each source's adapter reads, and how a diagnosis bisects.
- [Declarations: what a model means](../skills/transform/references/declarations.md):
  every field, and every test and monitor it renders.
- [Dex Semantics](dex-semantics.md): the declarations the rules come from.
- [The command contract](command-contract.md): `maintain incidents`,
  `maintain diagnose` and `maintain verify` in full.
