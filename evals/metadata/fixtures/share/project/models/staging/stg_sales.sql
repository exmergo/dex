select sale_id, sold_on, region, product_id, amount
from {{ source('raw', 'sales') }}
