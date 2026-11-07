**A full retirement runs when the reference scan's only limits are column
parses (#1172).** `transform remove var` with a retirement contract refused on
any limit, including SQL the column scan could not parse, which cannot hide a
`var()` read. It now plans the full contract there. `removed_column_still_read`
reads those files from compiled SQL where it can, and
`data.retirement.column_reads` names any file read neither way. That leaves the
verdict `unchecked` and is carried to the build as an unchecked obligation.
Unread YAML, an unreadable package and a dynamic `var()` read still refuse, and
`transform rename` and every other `remove` kind still refuse on any limit.
