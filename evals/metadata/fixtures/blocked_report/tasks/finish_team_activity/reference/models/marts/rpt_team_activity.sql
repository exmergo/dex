select
    t.team_id,
    t.team_name,
    coalesce(m.member_count, 0) as member_count,
    coalesce(m.active_member_count, 0) as active_member_count,
    coalesce(e.event_count, 0) as event_count
from {{ ref('stg_teams') }} as t
left join (
    select
        team_id,
        count(*) as member_count,
        count(*) filter (where left_on is null) as active_member_count
    from {{ ref('int_team_members') }}
    group by team_id
) as m on m.team_id = t.team_id
left join {{ ref('int_team_events') }} as e on e.team_id = t.team_id
