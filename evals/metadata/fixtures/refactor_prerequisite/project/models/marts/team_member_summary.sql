select
    t.team_id,
    t.team_name,
    coalesce(m.member_count, 0) as member_count,
    coalesce(h.handled_members, 0) as handled_members,
    m.first_joined_ts
from {{ ref('stg_teams') }} as t
left join (
    select team_id, {{ count_members('member_id') }} as member_count, min(joined_ts) as first_joined_ts
    from {{ ref('roster_member') }}
    group by team_id
) as m on m.team_id = t.team_id
left join (
    select r.team_id, count(*) as handled_members
    from {{ ref('roster_member') }} as r
    inner join {{ ref('stg_handles') }} as h on h.member_id = r.member_id
    group by r.team_id
) as h on h.team_id = t.team_id
