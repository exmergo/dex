select product_id, price
from {{ source('raw', 'product_prices') }}
