select customer_id, customer_name, region
from {{ source('raw', 'customers') }}
