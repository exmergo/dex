select
    p.product_id,
    p.product_name,
    p.supplier,
    l.price
from {{ ref('stg_products') }} as p
left join {{ ref('stg_product_prices') }} as l
    on p.product_id = l.product_id
