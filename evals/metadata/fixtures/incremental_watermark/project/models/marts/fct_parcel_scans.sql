{{ config(materialized='incremental') }}

select
    scan_id,
    parcel_id,
    hub_code,
    scanned_at
from {{ ref('stg_parcel_scans') }}
{% if is_incremental() %}
where scanned_at > (select max(scanned_at) from {{ this }})
{% endif %}
