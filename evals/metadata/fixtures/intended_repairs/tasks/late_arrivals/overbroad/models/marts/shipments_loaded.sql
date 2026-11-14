-- Over-broad: picks up the late shipments, and re-dates every shipment by its load.
select
    shipment_id,
    cast(loaded_at as date) as shipped_on,
    region,
    date_diff('day', shipped_on, cast(loaded_at as date)) as days_to_load
from {{ ref('stg_shipments') }}
