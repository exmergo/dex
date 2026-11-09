select snapshot_date, store_id, sku, units_on_hand
from {{ source('raw', 'stock_levels') }}
