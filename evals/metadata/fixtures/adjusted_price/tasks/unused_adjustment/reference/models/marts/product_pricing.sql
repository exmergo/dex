with lines as (
    select * from {{ ref('int_line_prices') }}
)

select
    product_id,
    count(*) as order_lines,
    avg(list_price) as avg_list_price,
    avg(adjusted_price) as avg_adjusted_price
from lines
group by product_id
