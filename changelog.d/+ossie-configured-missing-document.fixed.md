**Deriving an Ossie document at an already configured path produces a plan.**
When the document did not exist yet, `transform plan` and `transform declare`
only proposed its contents and incorrectly asked to add the same path to
`semantic.ossie.files` again. The derived document is now validated with the
configured layer and stored as a companion plan that `transform apply` can write.
Unconfigured paths still require explicit adoption.
