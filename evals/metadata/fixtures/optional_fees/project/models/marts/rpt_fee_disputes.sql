select
    d.dispute_id,
    d.order_id,
    o.merchant_id,
    d.opened_on,
    d.disputed_fee
from {{ ref('stg_fee_disputes') }} as d
left join {{ ref('fct_orders') }} as o on d.order_id = o.order_id
