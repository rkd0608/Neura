"""The change-event stream (design §20.6.1): resumable from a cursor, never skipping an event, and
expired (410) only when events after its cursor were pruned."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import psycopg
from test_api_conventions import new_project

from conftest import ConformanceClient

Api = tuple[ConformanceClient, str]
Events = list[dict[str, Any]]


def read_stream(
    client: ConformanceClient, project: str, cursor: str | None
) -> tuple[int, list[str], Events]:
    """One stream, read until the control plane ends it (the tests' streams last half a second):
    its status, every id it sent and its events."""
    headers = {"Authorization": f"Bearer {client.token}"}
    if cursor is not None:
        headers["Last-Event-ID"] = cursor
    ids: list[str] = []
    events: Events = []
    with client.client.stream("GET", f"/v1/projects/{project}/events", headers=headers) as response:
        if response.status_code != 200:
            response.read()
            return response.status_code, ids, events
        for line in response.iter_lines():
            if line.startswith("id: "):
                ids.append(line.removeprefix("id: "))
            elif line.startswith("data: "):
                events.append(json.loads(line.removeprefix("data: ")))
    return 200, ids, events


def follow(
    client: ConformanceClient,
    project: str,
    cursor: str | None,
    until: Callable[[list[str], Events], bool],
    timeout: float = 30.0,
) -> tuple[list[str], Events]:
    """Read the stream as a client does, reconnecting from the last id the control plane sent,
    until ``until(ids, events)``. An event shows only once every transaction that could still
    write one before it has ended, so it can take a stream or two."""
    ids: list[str] = []
    events: Events = []
    deadline = time.monotonic() + timeout
    while not until(ids, events):
        assert time.monotonic() < deadline, (ids, events)
        status, more_ids, more = read_stream(client, project, ids[-1] if ids else cursor)
        assert status == 200
        ids, events = ids + more_ids, events + more
    return ids, events


def newest_cursor(database_url: str) -> str:
    """The log's newest event. The API wrote these tests' events one request at a time, so it is
    also the last in the order the stream reads them."""
    with psycopg.connect(database_url) as conn:
        row = conn.execute("SELECT max(cursor) FROM events").fetchone()
    assert row is not None
    return str(row[0])


def test_event_stream_resumes_from_cursor(api: Api, database_url: str) -> None:
    client, org = api
    project = new_project(client, org)["metadata"]["id"]
    client.post(
        f"/v1/projects/{project}/sites",
        {"spec": {"name": "s", "display_name": "S", "offline_authorization_hours": 1}},
    )
    headers = {"Authorization": f"Bearer {client.token}"}
    with client.client.stream(
        "GET", f"/v1/projects/{project}/events", params={"cursor": "0"}, headers=headers
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = []
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(line)
                break
    assert '"type":"resource.created"' in events[0]
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("DELETE FROM events WHERE cursor = (SELECT min(cursor) FROM events)")
    expired = client.get(f"/v1/projects/{project}/events", params={"cursor": "0"}, validate=False)
    assert expired.status_code == 410
    assert expired.json()["reason"] == "API_CURSOR_EXPIRED"


def test_an_event_whose_transaction_commits_late_is_not_skipped(
    api: Api, database_url: str
) -> None:
    client, org = api
    project = new_project(client, org)["metadata"]["id"]
    _, ids, _ = read_stream(client, project, None)
    insert = (
        "INSERT INTO events (project_id, type, resource, resource_version) "
        "VALUES (%s, 'resource.updated', %s, '1') RETURNING cursor"
    )
    with psycopg.connect(database_url) as slow, psycopg.connect(database_url) as fast:
        late = slow.execute(insert, (project, "late")).fetchone()
        early = fast.execute(insert, (project, "early")).fetchone()
        fast.commit()
        assert late is not None
        assert early is not None
        assert late[0] < early[0]
        # A stream while the first transaction is still open, then the rest once it commits.
        _, during_ids, during = read_stream(client, project, ids[-1])
        slow.commit()
    _, after = follow(client, project, during_ids[-1], lambda _, after: len(during + after) >= 2)
    assert sorted(e["resource"] for e in during + after) == ["early", "late"]


def test_a_cursor_has_expired_only_when_events_after_it_were_pruned(
    api: Api, database_url: str
) -> None:
    client, org = api
    project = new_project(client, org)["metadata"]["id"]
    # A reader that has every event loses nothing when all of them are pruned: it resumes.
    caught_up = newest_cursor(database_url)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("DELETE FROM events")
    assert read_stream(client, project, caught_up)[0] == 200
    # One that events were written after, and pruned before it came back, has expired.
    client.post(
        f"/v1/projects/{project}/sites",
        {"spec": {"name": "s", "display_name": "S", "offline_authorization_hours": 1}},
    )
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("DELETE FROM events")
    expired = client.get(
        f"/v1/projects/{project}/events", params={"cursor": caught_up}, validate=False
    )
    assert expired.status_code == 410
    assert expired.json()["reason"] == "API_CURSOR_EXPIRED"


def test_a_quiet_projects_stream_cursor_follows_the_log(api: Api, database_url: str) -> None:
    client, org = api
    quiet = new_project(client, org, "quiet")["metadata"]["id"]
    _, ids, _ = read_stream(client, quiet, None)
    busy = new_project(client, org, "busy")["metadata"]["id"]
    client.post(
        f"/v1/projects/{busy}/sites",
        {"spec": {"name": "s", "display_name": "S", "offline_authorization_hours": 1}},
    )
    newest = newest_cursor(database_url)
    # The quiet project's stream moves its cursor past the busy project's events, so pruning
    # them does not expire it.
    _, events = follow(client, quiet, ids[-1], lambda ids, _: newest in ids)
    assert events == []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("DELETE FROM events")
    assert read_stream(client, quiet, newest)[0] == 200
