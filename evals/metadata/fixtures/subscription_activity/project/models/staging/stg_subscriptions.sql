select subscription_id, plan, started_at
from {{ source('raw', 'subscriptions') }}
