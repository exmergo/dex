**`dex maintain diagnose` takes each open test incident to a cause, or says no cause was confirmed.**
It reads the incidents as `dex maintain incidents` does and bisects each one
over its coverage timeline: after the last observed-good run, through the first
observed-bad one, where a run that recorded nothing widens the range and never
narrows it. The committed `maintain snapshot` series (the versions of
`.dex/snapshot.json` in the repository's history) bounds the break where there
is one; on a project's first day there is none, and the answer names the window
no recorded state of the warehouse covers. Each candidate cause is confirmed or
ruled out by a check Dex already has, named in the answer. This release checks
a uniqueness test failing because the loader delivered a key twice, with the
grain axis of `dex maintain check` on each source the tested model reads. With
no confirmed cause the answer lists what was ruled out and the unseen window,
and never names the likeliest candidate. A confirmed cause's repair is handed
to `transform` as a next step, never written. `DexEngine.diagnose()` returns the
same result.
