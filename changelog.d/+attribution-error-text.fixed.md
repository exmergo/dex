**`transform plan` row attribution no longer copies a warehouse error into
`reason`.** A failed cast's error quotes the value it could not convert, so a
value from the data could reach the envelope. The reason now keeps the context
and the exception type (#861).
On BigQuery, a prior model that did not compile also made its dry-run pricing raise
out of the batch, so nothing was measured. Each statement is now priced on its own.
