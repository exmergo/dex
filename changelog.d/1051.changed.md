**`maintain reconcile` cuts a Cube view source whose includes all go.**
when a dropped column removes every member in a view source's `includes` list,
reconcile cuts that whole source entry from `cubes` rather than leaving
`includes: []`. if that removal empties all sources for the view, the proposal
is kept advisory with an explanation instead of writing a plan that would
leave an empty view.
