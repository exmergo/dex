-- tiny_tasks 2.0: a task's due timestamp is its timestamped due_at where the
-- source has one, falling back to the date it is due on.
select
    task_id,
    task_name,
    coalesce(due_at, due_on) as due_ts
from {{ ref('task_data') }}
