import base64
import io
import zipfile

import pytest

from radar.connections import parse_connections, profile_slug, read_export
from radar.db import Store
from radar.models import RawPost, Verdict

CONNECTIONS = """Notes:
"When exporting your connection data, you may notice that some of the email addresses are missing."

First Name,Last Name,URL,Email Address,Company,Position,Connected On
Ada,Lovelace,https://www.linkedin.com/in/Ada-Lovelace,ada@example.com,Analytical,Engineer,01 Jan 2024
Grace,Hopper,,,US Navy,Admiral,02 Feb 2024
"""

INVITATIONS = """From,To,Sent At,Message,Direction,inviterProfileUrl,inviteeProfileUrl
Me,Alan Turing,"11/1/26, 10:00 AM",,OUTGOING,https://www.linkedin.com/in/me,https://www.linkedin.com/in/alan-turing
Someone,Me,"11/2/26, 9:00 AM",Hi,INCOMING,https://www.linkedin.com/in/someone,https://www.linkedin.com/in/me
"""


def _zip(**files) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(f"Basic_LinkedInDataExport/{name}", text)
    return buf.getvalue()


def _person(slug, name, degree="2nd"):
    return RawPost(
        key=slug,
        author=name,
        author_url=f"https://www.linkedin.com/in/{slug}/",
        author_kind="person",
        degree=degree,
        headline="Researcher",
        text=f"{name} posts a new MLIP preprint with code and data for molten salts.",
    )


@pytest.fixture
def store():
    s = Store(":memory:")
    v = Verdict("core", "preprint", ["mlip"], "A preprint.", "core")
    for slug, name in [
        ("ada-lovelace", "Ada Lovelace"),
        ("grace-hopper-123", "Grace Hopper"),
        ("alan-turing", "Alan Turing"),
        ("bob-builder", "Bob Builder"),
    ]:
        s.add_post(_person(slug, name), v, [])
    yield s
    s.close()


def queue_names(store, include_done=False):
    return sorted(r["name"] for r in store.queue_rows(include_done=include_done))


def test_profile_slug_normalizes():
    assert profile_slug("https://www.linkedin.com/in/Ada-Lovelace/?trk=x") == "ada-lovelace"
    assert profile_slug("linkedin.com/in/j%C3%BCrgen-m") == "jürgen-m"
    assert profile_slug("https://www.linkedin.com/company/argonne/") is None
    assert profile_slug("") is None and profile_slug(None) is None


def test_read_export_zip_and_csv():
    conns, pending = read_export(
        "export.zip", _zip(**{"Connections.csv": CONNECTIONS, "Invitations.csv": INVITATIONS})
    )
    assert [c["name"] for c in conns] == ["Ada Lovelace", "Grace Hopper"]
    assert conns[0]["slug"] == "ada-lovelace" and conns[1]["slug"] is None
    assert [p["slug"] for p in pending] == ["alan-turing"]  # incoming invites ignored
    conns, pending = read_export("Connections.csv", CONNECTIONS.encode())
    assert len(conns) == 2 and pending == []
    with pytest.raises(ValueError, match="not LinkedIn"):
        read_export("other.csv", b"a,b\n1,2\n")
    with pytest.raises(ValueError, match="ZIP"):
        read_export("export.zip", b"not a zip")
    with pytest.raises(ValueError, match="no Connections"):
        read_export("export.zip", _zip(**{"Messages.csv": "x"}))
    with pytest.raises(ValueError, match="upload"):
        read_export("export.pdf", b"x")


def test_import_removes_connections_and_pending_from_queue(store):
    assert queue_names(store) == ["Ada Lovelace", "Alan Turing", "Bob Builder", "Grace Hopper"]
    conns, pending = read_export(
        "x.zip", _zip(**{"Connections.csv": CONNECTIONS, "Invitations.csv": INVITATIONS})
    )
    res = store.import_connections(conns, pending)
    assert res == {"connections": 2, "pending": 1, "removed_from_queue": 3}
    assert queue_names(store) == ["Bob Builder"]  # Grace matched by name (no URL in export)
    status = dict(store.conn.execute("SELECT name, status FROM authors").fetchall())
    assert status["Alan Turing"] == "sent" and status["Ada Lovelace"] == "connected"
    dump = "\n".join(str(tuple(r)) for r in store.conn.execute("SELECT * FROM connections"))
    assert "ada@example.com" not in dump  # emails are never stored
    assert store.connection_summary()["connected"] == 2


def test_new_author_who_is_a_connection_never_enters_queue(store):
    store.import_connections(parse_connections(CONNECTIONS), [])
    v = Verdict("core", "paper", [], "s", "r")
    store.add_post(_person("ada-lovelace", "Ada Lovelace"), v, [])  # seen again
    store.add_post(_person("new-person", "New Person"), v, [])
    assert "Ada Lovelace" not in queue_names(store) and "New Person" in queue_names(store)


def test_reimport_upgrades_pending_and_feed_records_first_degree(store):
    conns, pending = read_export("i.csv", INVITATIONS.encode())
    store.import_connections(conns, pending)
    accepted = CONNECTIONS + "Alan,Turing,https://www.linkedin.com/in/alan-turing,,,,03 Mar 2026\n"
    store.import_connections(parse_connections(accepted), [])
    row = store.conn.execute("SELECT status FROM connections WHERE slug='alan-turing'").fetchone()
    assert row["status"] == "connected"
    # a pending import never downgrades a connection
    store.import_connections([], pending)
    row = store.conn.execute("SELECT status FROM connections WHERE slug='alan-turing'").fetchone()
    assert row["status"] == "connected"
    store.add_post(
        _person("bob-builder", "Bob Builder", degree="1st"),
        Verdict("core", "paper", [], "s", "r"),
        [],
    )
    src = store.conn.execute("SELECT source FROM connections WHERE slug='bob-builder'").fetchone()
    assert src["source"] == "feed" and "Bob Builder" not in queue_names(store)


def test_panel_marks_update_connections_and_undo_forgets(store):
    url = "https://www.linkedin.com/in/bob-builder/"
    store.set_author_status(url, "sent")
    assert (
        store.conn.execute("SELECT status FROM connections WHERE slug='bob-builder'").fetchone()[0]
        == "pending"
    )
    store.set_author_status(url, "todo")
    assert store.conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0] == 0
    assert "Bob Builder" in queue_names(store)
    store.set_author_status(url, "connected")
    assert "Bob Builder" not in queue_names(store)
    assert "Bob Builder" in queue_names(store, include_done=True)  # can still be undone
    store.import_connections(parse_connections(CONNECTIONS), [])
    assert "Ada Lovelace" not in queue_names(store, include_done=True)  # export rows stay hidden


def test_import_endpoint(tmp_path):
    import shutil

    from fastapi.testclient import TestClient

    from radar.config import REPO_ROOT, load_config
    from radar.ui.app import create_app
    from tests.test_ui import FakeRunner

    (tmp_path / "config").mkdir()
    shutil.copy(REPO_ROOT / "config" / "config.example.toml", tmp_path / "config")
    shutil.copy(REPO_ROOT / "config" / "field_profile.md", tmp_path / "config")
    cfg = load_config(root=tmp_path)
    cfg.data_dir = tmp_path / "data"
    client = TestClient(create_app(cfg, token="t", runner=FakeRunner()))
    h = {"X-Radar-Token": "t", "Origin": "http://testserver"}
    data = base64.b64encode(_zip(**{"Connections.csv": CONNECTIONS})).decode()
    r = client.post("/api/connections/import", json={"filename": "a.zip", "data": data}, headers=h)
    assert r.status_code == 200 and r.json()["connected"] == 2
    assert client.get("/api/connections").json()["last_import"]
    bad = client.post(
        "/api/connections/import", json={"filename": "a.zip", "data": "QUJD"}, headers=h
    )
    assert bad.status_code == 400
    assert (
        client.post("/api/connections/import", json={"filename": "a.zip", "data": data}).status_code
        == 403
    )
