select
    order_id,
    merchant_id,
    ordered_on,
    gross_amount
    {{ fee_columns() }}
from {{ ref('stg_orders') }}
