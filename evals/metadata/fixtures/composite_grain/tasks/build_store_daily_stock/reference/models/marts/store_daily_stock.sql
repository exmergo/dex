select
    store_id,
    snapshot_date,
    sum(units_on_hand) as units_on_hand,
    count(distinct case when units_on_hand > 0 then sku end) as skus_in_stock
from {{ ref('stg_stock_levels') }}
group by 1, 2
