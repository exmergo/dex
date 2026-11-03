select task_id, task_name, due_ts
from {{ ref('tasks_task') }}
