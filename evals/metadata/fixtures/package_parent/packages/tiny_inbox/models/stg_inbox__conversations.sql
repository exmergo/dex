select
    conversation_id,
    created_at,
    state
from {{ source('tiny_inbox', 'conversations') }}
