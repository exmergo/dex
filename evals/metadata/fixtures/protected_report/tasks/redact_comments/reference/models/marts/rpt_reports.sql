select report_id, cast(NULL as varchar) as comments from {{ ref('stg_reports') }}
