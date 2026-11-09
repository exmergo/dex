select
    dispute_id,
    order_id,
    opened_on,
    disputed_fee
from {{ source('raw', 'fee_disputes') }}
