select u.account_id, u.used_on, sum(u.units) as units, count(*) as usage_records
from {{ ref('stg_usage') }} u
inner join {{ ref('stg_accounts') }} a on a.account_id = u.account_id
where not a.is_sandbox
group by u.account_id, u.used_on
