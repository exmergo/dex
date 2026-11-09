{% macro count_users(column) %}count(distinct {{ column }}){% endmacro %}
