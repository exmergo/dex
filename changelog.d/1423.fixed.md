**A value a conversion error quotes is masked even when it holds a line break.**
`mask_values` matched one line at a time, so a quoted value that ran onto a
second line was complete on neither and both parts reached the envelope through
`failure_text`, and a warehouse refusal kept the first part. The line breaks
inside such a value are now folded before matching, and `warehouse_refusal`
masks the whole message before it keeps the first line.
