select
    r.team_id,
    {{ count_members('r.member_id') }} as member_count,
    count(h.member_id) as handled_members,
    min(r.joined_ts) as first_joined_ts
from {{ ref('roster_member') }} as r
left join {{ ref('stg_handles') }} as h on h.member_id = r.member_id
group by r.team_id
