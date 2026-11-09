select
    t.team_id,
    t.team_name,
    coalesce(m.member_count, 0) as member_count,
    coalesce(m.handled_members, 0) as handled_members,
    m.first_joined_ts
from {{ ref('stg_teams') }} as t
left join {{ ref('int_team_members') }} as m on m.team_id = t.team_id
