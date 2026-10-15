# neura-sdk

Python client for the Neura management API. Everything the console can do, this client can do:
it is generated from `api/openapi.yaml` (`make gen`), one method per operation.

```python
from neura_sdk import Client

with Client("https://neura.example.com", token="ntk_tok_...") as neura:
    me = neura.get_current_principal()
    for site in neura.paginate(neura.list_sites, project_id):
        print(site["metadata"]["id"], site["status"]["phase"])
    operation = neura.prepare_revision(revision_id)
    neura.wait_for_operation(operation["metadata"]["id"])
```

- POSTs get an idempotency key automatically, so retrying a call is safe.
- PATCH and DELETE take `if_match=` (the resource's `metadata.version`).
- Errors raise `NeuraApiError` carrying the reason code from the catalog.
- `429` and `503` are retried with backoff, honouring `Retry-After`.
