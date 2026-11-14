select
    line_id,
    product_id,
    ordered_on,
    list_price,
    discount_rate,
    is_refunded
from {{ source('raw', 'order_lines') }}
