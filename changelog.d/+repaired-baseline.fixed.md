**A refactor blocked by a failing prerequisite can now be compared once the repair is built, including by a `--cold-deps` build.**
`transform compare` judged a refactor's inputs by the ordinary build's
`run_results.json` alone, while a `--cold-deps` build writes its own under
`cold/`. A package prerequisite repaired and proven with `--cold-deps`, as the
procedure asks, still compared `inputs_unsynchronized`, naming the failure that
build had repaired. It now reads each upstream model's latest build attempt,
ordinary or cold, with the manifest of the newest; a compile no longer counts as
one. The `inputs_unsynchronized` detail names the order that clears it: the
repair in a plan of its own, applied and built (`--cold-deps` for a package's
model), then the refactor's plan compared while still unapplied. A recorded
verdict includes `inputs_built_by` (`invocation_id`, `generated_at`, `cold`) when
an upstream build exists: the latest build among the compared model's upstream
models, recorded on the plan and reported by `transform apply` and a
`--for-plan` build. It names one run; other inputs may come from earlier builds.
It is absent where no build attempted those models, and on
`awaiting_confirmation` and `plan_applied`, which are not recorded. `data.refactor_comparison_offer` on
`transform plan` now says before any comparison whether the baseline builds:
`baseline_ready`, and `per_model[].blocked_inputs` with each input's status and
owner, with `data.next` leading with the repair. A plan that carries the repair
beside the rewrite, whose comparison could only read the failed build, is named
in `mixed_with_repair` with a warning. `transform deps` and the shared procedure
say that installing a project's declared packages is not installing or upgrading
a tool. New eval task `refactor_prerequisite/package_blocked_refactor`, and two
procedure steps, `rebuilt_before_compare` and `cold_deps_after_repair` (#1114).
