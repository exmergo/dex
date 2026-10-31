select
    t.account_id,
    a.account_name,
    count(*) as ticket_count,
    max(t.opened_on) as last_ticket_opened_on
from {{ ref('stg_tickets') }} as t
left join {{ ref('stg_accounts') }} as a using (account_id)
group by 1, 2
