**Organization-name columns no longer hold a person-name PII flag.** A `*_name`
column qualified by an organization (`team_name`, `lab_name`, `org_name`,
`vendor_name` and similar) is no longer flagged as a person's name, and a
generic name column whose values mostly end in an organization suffix
("Acme Inc", "Stability AI", "Bell Labs") is de-rated below the query
firewall's threshold by a new value-shape statistic, which also stops such
values counting as person-shaped. Every refined name flag now reports the rule
that decided it as `refinement` (`person_shape`, `organization`, `unsettled`,
...), and a refusal names the verdict of a flag that still blocks (#647).
