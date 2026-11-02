select event_id, team_id, occurred_on
from {{ source('raw', 'events') }}
where source_id = 27
