select
    a.region,
    count(distinct a.account_id) as accounts,
    coalesce(sum(u.units), 0) as units
from {{ ref('stg_accounts') }} a
left join {{ ref('stg_usage') }} u on u.account_id = a.account_id
where not a.is_sandbox
group by a.region
