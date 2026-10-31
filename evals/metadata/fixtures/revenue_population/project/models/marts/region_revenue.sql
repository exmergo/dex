select
    c.region,
    sum(o.amount) as revenue
from {{ ref('stg_orders') }} as o
join {{ ref('stg_customers') }} as c using (customer_id)
where o.status not in ('cancelled', 'returned')
group by 1
