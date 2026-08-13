import duckdb
c = duckdb.connect("demo199/lone.duckdb")
c.execute("CREATE TABLE t (id INTEGER)")
c.execute("INSERT INTO t VALUES (1)")
c.close()
