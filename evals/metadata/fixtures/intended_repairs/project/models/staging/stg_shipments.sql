select shipment_id, shipped_on, loaded_at, region from {{ source('raw', 'shipments') }}
