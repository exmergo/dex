select region, sum(amount) as revenue
from {{ ref('stg_sales') }}
group by 1
