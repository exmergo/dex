-- Over-broad: the inner join also drops memberships whose workspace is not listed.
select m.membership_id, m.workspace_id, m.user_id
from {{ ref('stg_memberships') }} m
join {{ ref('stg_workspaces') }} w on w.workspace_id = m.workspace_id
where not w.is_archived
