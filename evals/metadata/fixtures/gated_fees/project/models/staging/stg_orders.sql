select
    order_id,
    merchant_id,
    ordered_on,
    gross_amount,
    platform_fee
from {{ source('raw', 'orders') }}
