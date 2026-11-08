select
    opened_on as ticket_date,
    count(*) as tickets
from {{ ref('stg_tickets') }}
group by opened_on
