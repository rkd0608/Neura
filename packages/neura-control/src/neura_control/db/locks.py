"""Row locks shared by the services that change the same rows."""

from __future__ import annotations

from typing import Any

# Site rows are locked FOR NO KEY UPDATE: it serializes changes to a site's state but not the
# foreign-key checks of rows that reference the site (operations, revisions, commands), which
# would otherwise deadlock with requests that lock a deployment and then start an operation.
SITE_ROW: dict[str, Any] = {"key_share": True}
