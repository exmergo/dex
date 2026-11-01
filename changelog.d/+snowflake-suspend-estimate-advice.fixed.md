**A Snowflake estimate no longer recommends `snowflake.suspend_after_command`
when it is already on.** With the setting on, dex still quotes a project's first
command that resumes the warehouse the whole `AUTO_SUSPEND` tail, because it
counts on a suspend only once it has seen one succeed. The note beside the
estimate now says so, and says that later commands get the resume minimum. The
advice next to it used to recommend turning the setting on and running
`GRANT OPERATE` even when both were done. It now names the `dex config set` only
while the setting is off and the grant only after a suspend was refused for
lacking it, and dex drops it once a suspend keeps the tail out of the quote.
