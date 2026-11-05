**`transform test --scaffold` now places boundary rows for day windows over whole-day expressions.**
BigQuery has no interval `RANGE` frames, so day windows are commonly written as
numeric `RANGE` frames over epoch-day expressions such as `unix_date(pickup_date)`
or `date_diff(pickup_date, date '1970-01-01', day)`. The scaffold now recognizes an
`ORDER BY` expression that is a whole-day function of one date column and treats
the numeric bound as days, placing the same four boundary rows (anchor, last
included day, first excluded day, and a gap in calendar dates) as an interval frame
does. Any numeric `RANGE` over another expression keeps the `numeric_range_offset`
warning note (#1135).
