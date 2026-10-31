select
    order_date,
    count(*) as order_count,
    sum(amount) as revenue
from {{ ref('stg_orders') }}
where status not in ('cancelled', 'returned')
group by 1
