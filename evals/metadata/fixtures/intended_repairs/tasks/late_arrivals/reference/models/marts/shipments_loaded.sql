select
    shipment_id,
    shipped_on,
    region,
    date_diff('day', shipped_on, cast(loaded_at as date)) as days_to_load
from {{ ref('stg_shipments') }}
