# Connector: BigQuery (stub)

Implemented in Phase 4. Large-landscape pain; bytes-scanned billing, so the cost
guard uses dry-run, `maximum_bytes_billed`, sampling, and a per-session byte
budget (raw preview via `tabledata.list` is free). Auth via Application Default
Credentials, never a pasted service-account key. Namespace:
`project.dataset.table`.
