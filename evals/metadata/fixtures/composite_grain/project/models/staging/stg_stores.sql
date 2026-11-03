select store_id, store_name
from {{ source('raw', 'stores') }}
