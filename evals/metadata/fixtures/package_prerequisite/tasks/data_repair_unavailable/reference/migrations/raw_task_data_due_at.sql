-- NOT RUN. A proposal for the ingestion vendor, who owns raw.task_data.
-- The tiny_tasks package expects due_at to be a timestamp. The project builds
-- today through a project override; this change to the source is still owed.
alter table raw.task_data alter column due_at type timestamp
    using cast(cast(due_at as varchar) as timestamp);
