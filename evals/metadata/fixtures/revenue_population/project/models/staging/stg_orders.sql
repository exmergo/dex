select
    order_id,
    customer_id,
    ordered_at as order_date,
    status,
    amount
from {{ source('raw', 'orders') }}
