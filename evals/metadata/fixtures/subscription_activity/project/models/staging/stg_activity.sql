select activity_id, subscription_id, occurred_at
from {{ source('raw', 'activity') }}
