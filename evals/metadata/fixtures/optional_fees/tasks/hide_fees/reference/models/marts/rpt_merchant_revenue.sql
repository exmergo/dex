select
    merchant_id,
    count(*) as order_count,
    sum(gross_amount) as gross_revenue
    {% if var('enable_platform_fees') %}
    , sum(platform_fee) as platform_fees
    , sum(net_amount) as net_revenue
    {% endif %}
from {{ ref('fct_orders') }}
group by merchant_id
