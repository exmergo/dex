-- Project override of tiny_tasks.tasks_task, which dbt_project.yml disables.
-- due_at arrives all NULL and typed integer, which the package's coalesce with
-- the due_on timestamp cannot take. Cast through text so it keeps its meaning
-- as a timestamp, and keep every task.
select
    task_id,
    task_name,
    coalesce(cast(cast(due_at as varchar) as timestamp), due_on) as due_ts
from {{ ref('task_data') }}
