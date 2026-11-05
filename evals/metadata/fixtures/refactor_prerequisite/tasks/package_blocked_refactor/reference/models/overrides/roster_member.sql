-- Project override of tiny_roster.roster_member, which dbt_project.yml disables.
-- joined_at arrives all NULL and typed integer, which the package's coalesce
-- with the joined_on timestamp cannot take. Cast through text so it keeps its
-- meaning as a timestamp, and keep every member.
select
    member_id,
    team_id,
    member_name,
    coalesce(cast(cast(joined_at as varchar) as timestamp), joined_on) as joined_ts
from {{ ref('member_data') }}
