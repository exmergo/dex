select
    part_id,
    conversation_id,
    created_at as part_created_at
from {{ source('tiny_inbox', 'conversation_parts') }}
