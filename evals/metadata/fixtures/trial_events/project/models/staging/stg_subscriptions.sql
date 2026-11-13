select
    subscription_id,
    plan_name,
    started_on,
    cancelled_on,
    is_internal
from {{ source('raw', 'subscriptions') }}
