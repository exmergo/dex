select
    c.customer_id,
    c.region,
    count(o.order_id) as order_count,
    coalesce(sum(o.amount), 0) as lifetime_value
from {{ ref('stg_customers') }} as c
left join {{ ref('stg_orders') }} as o
    on o.customer_id = c.customer_id
    and o.status not in ('cancelled', 'returned')
group by 1, 2
