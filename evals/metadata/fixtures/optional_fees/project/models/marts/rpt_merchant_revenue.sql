select
    merchant_id,
    count(*) as order_count,
    sum(gross_amount) as gross_revenue,
    sum(platform_fee) as platform_fees,
    sum(net_amount) as net_revenue
from {{ ref('fct_orders') }}
group by merchant_id
