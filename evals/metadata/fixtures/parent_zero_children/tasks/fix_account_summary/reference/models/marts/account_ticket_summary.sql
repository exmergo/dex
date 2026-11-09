select
    a.account_id,
    a.account_name,
    count(t.ticket_id) as ticket_count,
    max(t.opened_on) as last_ticket_opened_on
from {{ ref('stg_accounts') }} as a
left join {{ ref('stg_tickets') }} as t using (account_id)
group by 1, 2
