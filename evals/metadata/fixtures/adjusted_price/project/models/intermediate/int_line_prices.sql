select
    line_id,
    product_id,
    is_refunded,
    list_price,
    list_price * (1 - discount_rate) as adjusted_price
from {{ ref('stg_order_lines') }}
