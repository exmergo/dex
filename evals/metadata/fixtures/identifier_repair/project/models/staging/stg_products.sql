select product_id, product_name, supplier
from {{ source('raw', 'products') }}
where supplier <> 'retired'
