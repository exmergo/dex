with by_product as (
    select s.region, s.product_id, p.product_name, sum(s.amount) as revenue
    from {{ ref('stg_sales') }} as s
    join {{ ref('stg_products') }} as p using (product_id)
    group by 1, 2, 3
)

select
    region,
    product_id,
    product_name,
    revenue,
    revenue / sum(revenue) over (partition by region) as share_of_region
from by_product
