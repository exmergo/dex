select review_id, listing_id, reviewed_on, rating
from {{ source('raw', 'reviews') }}
