-- tiny_roster 3.0: a member's join timestamp is its timestamped joined_at where
-- the source has one, falling back to the date the member joined on.
select
    member_id,
    team_id,
    member_name,
    coalesce(joined_at, joined_on) as joined_ts
from {{ ref('member_data') }}
