import sys
sys.path.insert(0, "tests")
from fakes.bigquery import FakeBigQueryClient, FakeTable
from google.cloud import bigquery
from exmergo_dex_core.adapters.bigquery import BigQueryAdapter
from exmergo_dex_core.config import BigQueryTarget
from exmergo_dex_core.guards.cost_guard import CostGate
from exmergo_dex_core.envelope import Paradigm

MB = 1024 * 1024

tables = [
    FakeTable(project="test-proj", dataset_id="shop", table_id="customers",
        schema=[bigquery.SchemaField("id", "INTEGER", mode="REQUIRED"),
                bigquery.SchemaField("email", "STRING")],
        num_rows=100, num_bytes=5_000),
]
client = FakeBigQueryClient(project="test-proj", tables=tables)

gate = CostGate(paradigm=Paradigm.BYTES_SCANNED, ceiling=500*MB, session_ceiling=None,
                session_spent=0.0, confirmed=True, connector="bigquery", command="explore")
adapter = BigQueryAdapter(project="test-proj", cost_gate=gate, target=BigQueryTarget(),
                           client=client, principal_type="user")

total, per_table = adapter.profile_estimate(["test-proj.shop.customers"])
print("estimate:", total, "bytes  (", total / MB, "MB )")
print("per_table:", per_table)
