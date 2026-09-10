"""File exploration: what a document collection holds and what is already known
about processing it.

A file collection (a BigQuery object table, a Snowflake directory table, a
Databricks volume or manifest) is an index of files in object storage, and the
evidence worth having about it is usually a table some earlier pipeline
materialized by running a document parser. This package is the contract both are
read through. ``contract`` holds the fixed vocabularies, the optional interfaces a
connector may implement, the request types, and the capability model; ``results``
holds the aggregate types that are the only thing a file operation returns.

Two constraints shape everything here. Document content never leaves the
warehouse: every content computation is a warehouse expression, and the result
types have no field that could hold a document body, an extracted value, a file
path, or a provider's error text. And nothing here invokes document processing:
dex assesses results that already exist, because a new processing charge would
need a provider-enforced hard spend cap that no supported path offers.
"""

from __future__ import annotations
