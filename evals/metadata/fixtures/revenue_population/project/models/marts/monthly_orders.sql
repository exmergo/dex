select
    date_trunc('month', order_date) as order_month,
    count(*) as order_count,
    sum(amount) as revenue
from {{ ref('stg_orders') }}
where status not in ('cancelled', 'returned')
group by 1
