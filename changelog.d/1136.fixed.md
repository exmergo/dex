**A governed Cube query now holds each cube to its own table.** Cube's `/v1/meta`
carries no `sql_table`, so a plan was refused only when it read a table no cube in
the repository sits on. A Cube server whose cubes swapped tables passed. Cube
writes each cube's relation as `FROM <table> AS "<cube>"`, so the plan is now
refused when a cube is read from another table than the `sql_table` its file
declares.
