select team_id, count(*) as event_count
from (
select team_id from {{ ref('stg_events_source_00') }}
union all
select team_id from {{ ref('stg_events_source_01') }}
union all
select team_id from {{ ref('stg_events_source_02') }}
union all
select team_id from {{ ref('stg_events_source_03') }}
union all
select team_id from {{ ref('stg_events_source_04') }}
union all
select team_id from {{ ref('stg_events_source_05') }}
union all
select team_id from {{ ref('stg_events_source_06') }}
union all
select team_id from {{ ref('stg_events_source_07') }}
union all
select team_id from {{ ref('stg_events_source_08') }}
union all
select team_id from {{ ref('stg_events_source_09') }}
union all
select team_id from {{ ref('stg_events_source_10') }}
union all
select team_id from {{ ref('stg_events_source_11') }}
union all
select team_id from {{ ref('stg_events_source_12') }}
union all
select team_id from {{ ref('stg_events_source_13') }}
union all
select team_id from {{ ref('stg_events_source_14') }}
union all
select team_id from {{ ref('stg_events_source_15') }}
union all
select team_id from {{ ref('stg_events_source_16') }}
union all
select team_id from {{ ref('stg_events_source_17') }}
union all
select team_id from {{ ref('stg_events_source_18') }}
union all
select team_id from {{ ref('stg_events_source_19') }}
union all
select team_id from {{ ref('stg_events_source_20') }}
union all
select team_id from {{ ref('stg_events_source_21') }}
union all
select team_id from {{ ref('stg_events_source_22') }}
union all
select team_id from {{ ref('stg_events_source_23') }}
union all
select team_id from {{ ref('stg_events_source_24') }}
union all
select team_id from {{ ref('stg_events_source_25') }}
union all
select team_id from {{ ref('stg_events_source_26') }}
union all
select team_id from {{ ref('stg_events_source_27') }}
union all
select team_id from {{ ref('stg_events_source_28') }}
union all
select team_id from {{ ref('stg_events_source_29') }}
union all
select team_id from {{ ref('stg_events_source_30') }}
union all
select team_id from {{ ref('stg_events_source_31') }}
union all
select team_id from {{ ref('stg_events_source_32') }}
union all
select team_id from {{ ref('stg_events_source_33') }}
union all
select team_id from {{ ref('stg_events_source_34') }}
union all
select team_id from {{ ref('stg_events_source_35') }}
union all
select team_id from {{ ref('stg_events_source_36') }}
union all
select team_id from {{ ref('stg_events_source_37') }}
union all
select team_id from {{ ref('stg_events_source_38') }}
union all
select team_id from {{ ref('stg_events_source_39') }}
) as e
group by team_id
