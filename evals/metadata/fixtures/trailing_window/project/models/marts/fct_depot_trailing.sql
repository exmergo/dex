select
    depot,
    pickup_date,
    parcels,
    sum(parcels) over (
        partition by depot
        order by pickup_date
        range between interval '6 day' preceding and current row
    ) as parcels_7d
from {{ ref('stg_pickups') }}
