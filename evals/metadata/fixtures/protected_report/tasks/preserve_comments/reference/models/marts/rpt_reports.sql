select report_id, comments from {{ ref('stg_reports') }}
