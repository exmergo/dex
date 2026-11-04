-- The feed re-sends a batch when a scanner reconnects; a re-sent scan is an
-- identical row, so distinct keeps one.
select distinct
    scan_id,
    parcel_id,
    upper(hub) as hub_code,
    scanned_at
from {{ source('raw', 'parcel_scans') }}
