**Dex states the Elementary releases it supports, and reports a result it cannot vouch for on any other release as unchecked.**
The supported range is the releases the Elementary fixture runs, from the oldest
to the newest (0.15.2 to 0.26.1 today), and the engine's list is held to the
fixture's by a test. On a release outside it, a patch above the newest included,
or one Dex cannot read from the installed package's `dbt_project.yml`, Dex no
longer acts on what the package does as if the release were one it knows:

- `transform build` lists every passing Elementary test whose compiled SQL is
  not the package's no-results query under
  `data.completion.assurance.unchecked_tests` with `elementary_release_untested`
  or `elementary_release_unknown`, so none counts toward a model's assurance,
  and a warning names the release read and the newest tested.
- The unpriced Elementary work (#1309) is counted wherever it may run, a default
  `days_back` is `unknown`, and `data.unpriced` carries `release` and
  `release_reason_code`.
- The #1381 sampling warning also fires above the newest tested release.
- A mutation run still makes the copy inert the same way, and warns.

A consumer that reads `assurance.unchecked_tests` reason codes or `data.unpriced`
sees the two new codes and fields only on a release outside the range.
