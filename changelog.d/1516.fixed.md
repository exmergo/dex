**A population filter stating both `includes` and `excludes` is judged on each
list.** `maintain verify` and `transform build --verify` compared the
`excludes` check against the `includes` values, so every included value read as
a contradiction. Each list is now its own rule with its own finding.
