**Deleting a project override of a package model is no longer refused.** A plan
that deletes a project model overriding a package model, and removes the
`+enabled: false` that disabled the package's own copy, was refused because a
surviving `ref()` still named the model. The package's model answers that `ref()`
once it is enabled again, so the delete guard now accepts it. It is the step that
undoes a project adaptation, for instance when the request turns out to be a repair
to the data. A deletion that leaves the package model disabled is still refused.
