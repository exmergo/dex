select
    merchant_id,
    {% if var('enable_platform_fees') %}platform_fee >= 10 as high_fee,{% endif %}
    count(*) as order_count,
    sum(gross_amount) as gross_revenue
    {% if var('enable_platform_fees') %}, sum(platform_fee) as platform_fees{% endif %}
from {{ ref('fct_orders') }}
group by {% if var('enable_platform_fees') %}1, 2{% else %}1{% endif %}
