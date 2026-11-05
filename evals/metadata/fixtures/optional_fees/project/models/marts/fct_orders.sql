select
    order_id,
    merchant_id,
    ordered_on,
    gross_amount,
    platform_fee,
    gross_amount - platform_fee as net_amount
from {{ ref('stg_orders') }}
