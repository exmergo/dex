select
    order_id,
    merchant_id,
    ordered_on,
    gross_amount
    {% if var('enable_platform_fees') %}
    , platform_fee
    , gross_amount - platform_fee as net_amount
    {% endif %}
from {{ ref('stg_orders') }}
