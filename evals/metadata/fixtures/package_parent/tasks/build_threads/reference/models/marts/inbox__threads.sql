with parts as (
    select
        conversation_id,
        count(*) as part_count,
        max(part_created_at) as last_part_at
    from {{ ref('stg_inbox__conversation_parts') }}
    group by conversation_id
)

select
    c.conversation_id,
    c.state,
    coalesce(p.part_count, 0) as part_count,
    p.last_part_at
from {{ ref('stg_inbox__conversations') }} c
left join parts p on p.conversation_id = c.conversation_id
