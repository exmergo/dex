select
    depot,
    pickup_date,
    parcels
from {{ source('raw', 'pickups') }}
