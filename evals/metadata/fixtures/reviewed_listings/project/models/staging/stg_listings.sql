select listing_id, listing_name, city
from {{ source('raw', 'listings') }}
