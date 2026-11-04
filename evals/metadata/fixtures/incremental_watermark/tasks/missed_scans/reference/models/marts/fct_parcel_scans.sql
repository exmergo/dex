{{
    config(
        materialized='incremental',
        unique_key='scan_id',
        incremental_strategy='delete+insert',
    )
}}

select
    scan_id,
    parcel_id,
    hub_code,
    scanned_at
from {{ ref('stg_parcel_scans') }}
{% if is_incremental() %}
-- `>=`, not `>`: a scan delivered late with the latest timestamp already
-- loaded is selected again, and unique_key keeps it from loading twice.
-- coalesce: max() over an empty table is NULL, which no row compares above.
where scanned_at >= coalesce(
    (select max(scanned_at) from {{ this }}),
    cast('1900-01-01' as timestamp)
)
{% endif %}
