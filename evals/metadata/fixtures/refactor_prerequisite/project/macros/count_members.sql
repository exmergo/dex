{% macro count_members(column) %}count(distinct {{ column }}){% endmacro %}
