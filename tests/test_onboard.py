import base64
import json
import shutil
import zipfile

import pytest

from radar import onboard
from radar.config import REPO_ROOT, load_config
from radar.models import RawPost, Verdict

CV = (
    "Jane Doe\njane.doe@uni.edu | +1 (555) 123-4567 | linkedin.com/in/janedoe\n\n"
    "PhD candidate, Chemical Engineering, 2021 - 2026.\n"
    "DOI: 10.1016/j.commatsci.2024.113410. PCCP 23(17):10533-10544.\n"
    "Research: machine-learning potentials for zeolite catalysis, DFT with VASP, "
    "active learning, enhanced sampling with PLUMED. " * 3
)

PROFILE = (
    "# Field profile\n\nCatalysis.\n\n## Core (always save)\n\n- zeolites\n\n"
    "## Adjacent (save, lower priority)\n\n- HPC\n\n## Skip\n\n- ads\n"
)


class Recorder:
    def __init__(self, result=None):
        self.tasks = []
        self.result = result or {
            "reader": "a catalysis PhD student",
            "profile_markdown": PROFILE,
            "notes": "ok",
        }

    def __call__(self, task, schema):
        self.tasks.append(task)
        assert schema is onboard.PROFILE_SCHEMA
        return self.result


@pytest.fixture
def root(tmp_path):
    (tmp_path / "config").mkdir()
    shutil.copy(REPO_ROOT / "config" / "config.example.toml", tmp_path / "config")
    (tmp_path / "config" / "field_profile.md").write_text("old profile\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def ocfg(root):
    c = load_config(root=root)
    c.data_dir = root / "data"
    return c


def test_redact_removes_contact_details_keeps_dates():
    out = onboard.redact(CV)
    assert "jane.doe@uni.edu" not in out and "555" not in out and "janedoe" not in out
    assert "[email]" in out and "[phone]" in out and "[profile link]" in out
    assert "2021 - 2026" in out and "VASP" in out
    assert "j.commatsci.2024.113410" in out and "10533-10544" in out


def test_extract_docx_and_txt(tmp_path):
    docx = tmp_path / "cv.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr(
            "word/document.xml",
            "<w:document><w:p><w:r><w:t>Zeolite &amp; MOF catalysis</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>VASP</w:t></w:r></w:p></w:document>",
        )
    assert onboard.extract_cv_text(docx) == "Zeolite & MOF catalysis\nVASP"
    txt = tmp_path / "cv.txt"
    txt.write_text("a   b\n\n\n\nc", encoding="utf-8")
    assert onboard.extract_cv_text(txt) == "a b\n\nc"
    with pytest.raises(ValueError):
        onboard.extract_cv_text(tmp_path / "cv.exe")


def test_save_cv_redacts_and_validates(ocfg):
    cv = onboard.save_cv(ocfg, "My CV.txt", CV.encode())
    text = (onboard.onboarding_dir(ocfg) / "cv.txt").read_text(encoding="utf-8")
    assert cv["name"] == "My CV.txt" and cv["chars"] == len(text)
    assert "jane.doe" not in text
    with pytest.raises(ValueError, match="unsupported"):
        onboard.save_cv(ocfg, "cv.exe", b"x" * 500)
    with pytest.raises(ValueError, match="almost no text"):
        onboard.save_cv(ocfg, "cv.txt", b"too short")
    with pytest.raises(ValueError, match="8 MB"):
        onboard.save_cv(ocfg, "cv.txt", b"x" * (onboard.CV_MAX_BYTES + 1))
    # only the redacted text is kept, and a failed replacement leaves the old CV in place
    assert sorted(p.name for p in onboard.onboarding_dir(ocfg).iterdir()) == ["cv.txt"]
    assert (onboard.onboarding_dir(ocfg) / "cv.txt").read_text(encoding="utf-8") == text


def test_calibrating_a_refined_profile_makes_it_the_base(ocfg):
    state = onboard.empty_state()
    state.update(
        draft={"reader": "old", "profile": "old"}, refined={"reader": "new", "profile": PROFILE}
    )
    state["corrections"] = {"0": "keep"}
    onboard.save_state(ocfg, state)
    post = _post("A", "New zeolite ML potential preprint with open code and data.")
    onboard.calibrate(
        ocfg, [post], lambda p: Verdict("core", "paper", [], "s", "r"), "live", log=lambda _: None
    )
    after = onboard.load_state(ocfg)
    assert after["draft"]["reader"] == "new" and after["refined"] is None
    assert after["corrections"] == {} and len(after["calibration"]["posts"]) == 1


def test_draft_uses_material_and_resets_later_steps(ocfg):
    state = onboard.load_state(ocfg)
    state.update(notes="I also follow MOFs.", calibration={"posts": []}, corrections={"1": "keep"})
    onboard.save_state(ocfg, state)
    onboard.save_cv(ocfg, "cv.txt", CV.encode())
    state = onboard.load_state(ocfg)
    state["cv"] = {"name": "cv.txt", "chars": 1}
    onboard.save_state(ocfg, state)
    rec = Recorder()
    draft = onboard.draft_profile(ocfg, model=rec, log=lambda _: None)
    assert draft["reader"] == "a catalysis PhD student" and draft["profile"].endswith("\n")
    assert "PLUMED" in rec.tasks[0] and "MOFs" in rec.tasks[0] and "jane.doe" not in rec.tasks[0]
    after = onboard.load_state(ocfg)
    assert after["draft"]["reader"] == draft["reader"]
    assert after["calibration"] is None and after["corrections"] == {}


def test_draft_needs_material_and_complete_output(ocfg):
    with pytest.raises(ValueError, match="upload a CV"):
        onboard.draft_profile(ocfg, model=Recorder(), log=lambda _: None)
    state = onboard.load_state(ocfg)
    state["notes"] = "Catalysis."
    onboard.save_state(ocfg, state)
    bad = Recorder({"reader": "x", "profile_markdown": "no headings", "notes": ""})
    with pytest.raises(RuntimeError, match="incomplete"):
        onboard.draft_profile(ocfg, model=bad, log=lambda _: None)


def _post(author, text, promoted=False):
    return RawPost(
        "k" + author, author, None, "person", None, "Researcher", text, promoted=promoted
    )


def test_calibrate_skips_ads_never_touches_db(ocfg):
    posts = [
        _post("A", "New zeolite ML potential preprint with open code and data."),
        _post("B", "Promoted: buy our GPUs today, limited offer for labs.", promoted=True),
        _post("C", "Congratulations to our team on the award, very proud of everyone."),
    ]

    def classify(p):
        tier = "core" if "zeolite" in p.text else "skip"
        return Verdict(tier, "paper", [], f"summary {p.author}", "reason")

    res = onboard.calibrate(ocfg, posts, classify, "stored", log=lambda _: None)
    assert [c["author"] for c in res["posts"]] == ["A", "C"]
    assert res["posts"][0]["tier"] == "core" and res["source"] == "stored"
    assert not ocfg.db_path.exists()
    with pytest.raises(RuntimeError, match="no posts"):
        onboard.calibrate(ocfg, posts[1:2], classify, "live", log=lambda _: None)


def test_corrections_keep_only_flips_and_refine_sends_them(ocfg):
    state = onboard.load_state(ocfg)
    state["draft"] = {"reader": "r", "profile": PROFILE}
    state["calibration"] = {
        "posts": [
            {"id": 0, "author": "A", "headline": "h", "text": "zeolite paper", "tier": "core"},
            {"id": 1, "author": "B", "headline": "h", "text": "MOF dataset", "tier": "skip"},
        ]
    }
    onboard.save_state(ocfg, state)
    with pytest.raises(ValueError, match="no corrections"):
        onboard.refine_profile(ocfg, model=Recorder(), log=lambda _: None)
    clean = onboard.set_corrections(ocfg, {"0": "keep", "1": "keep", "9": "skip", "0x": "maybe"})
    assert clean == {"1": "keep"}
    rec = Recorder()
    refined = onboard.refine_profile(ocfg, model=rec, log=lambda _: None)
    assert "filter said skip, reader says keep" in rec.tasks[0] and "MOF dataset" in rec.tasks[0]
    assert "[keep] A" in rec.tasks[0]
    assert onboard.current_profile(onboard.load_state(ocfg)) == refined


def test_apply_backs_up_and_saves_reader(ocfg, root):
    backup = onboard.apply_profile(ocfg, "  a catalysis   PhD student ", PROFILE)
    assert backup and backup.read_text(encoding="utf-8") == "old profile\n"
    assert (root / "config" / "field_profile.md").read_text(encoding="utf-8") == PROFILE
    assert 'reader = "a catalysis PhD student"' in (root / "config" / "config.toml").read_text(
        encoding="utf-8"
    )
    assert onboard.load_state(ocfg)["applied_at"]
    with pytest.raises(ValueError):
        onboard.apply_profile(ocfg, "", PROFILE)


def test_state_survives_corrupt_file(ocfg):
    onboard.onboarding_dir(ocfg).mkdir(parents=True)
    (onboard.onboarding_dir(ocfg) / "state.json").write_text("{bad", encoding="utf-8")
    assert onboard.load_state(ocfg) == onboard.empty_state()


# ---- panel endpoints ------------------------------------------------------------------------

TOKEN = "t0ken"
H = {"X-Radar-Token": TOKEN, "Origin": "http://testserver"}


@pytest.fixture
def client(root, monkeypatch):
    from fastapi.testclient import TestClient

    from radar.ui.app import create_app
    from tests.test_ui import FakeRunner

    c = load_config(root=root)
    c.data_dir = root / "data"
    runner = FakeRunner()
    tc = TestClient(create_app(c, token=TOKEN, runner=runner))
    tc.runner = runner
    return tc


def test_wizard_endpoints_flow(client, root):
    o = client.get("/api/onboarding").json()
    assert (
        o["first_run"] is True and o["state"]["draft"] is None and o["profile"] == "old profile\n"
    )
    data = base64.b64encode(CV.encode()).decode()
    r = client.post("/api/onboarding/cv", json={"filename": "cv.txt", "data": data}, headers=H)
    assert r.status_code == 200 and r.json()["cv"]["chars"] > 200
    assert (
        client.post(
            "/api/onboarding/cv", json={"filename": "cv.txt", "data": "%%"}, headers=H
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/onboarding/notes", json={"notes": "MOFs", "drafter": "gpt"}, headers=H
        ).status_code
        == 400
    )
    assert client.post(
        "/api/onboarding/notes", json={"notes": "MOFs", "drafter": "local"}, headers=H
    ).json()["saved"]
    assert client.get("/api/onboarding").json()["state"]["drafter"] == "local"

    r = client.post("/api/jobs", json={"action": "onboard-calibrate", "source": "web"}, headers=H)
    assert r.status_code == 400
    client.post(
        "/api/jobs",
        json={"action": "onboard-calibrate", "source": "stored", "max_posts": 999},
        headers=H,
    )
    assert client.runner.started[-1] == (
        "onboard-calibrate",
        ["--source", "stored", "--max-posts", "150"],
    )
    # while a wizard job runs, wizard edits are refused so its result cannot clobber them
    body = {"reader": "a catalysis student", "profile": PROFILE}
    assert client.post("/api/onboarding/apply", json=body, headers=H).status_code == 409
    assert client.post("/api/onboarding/reset", json={}, headers=H).status_code == 409
    client.runner.current.finished = 1.0

    r = client.post(
        "/api/onboarding/apply",
        json={"reader": "a catalysis student", "profile": PROFILE},
        headers=H,
    )
    assert r.status_code == 200 and r.json()["backup"].startswith("field_profile-")
    assert (root / "config" / "field_profile.md").read_text(encoding="utf-8") == PROFILE
    assert client.post("/api/onboarding/reset", json={}, headers=H).json()["reset"]
    assert client.get("/api/onboarding").json()["state"]["cv"] is None


def test_edit_updates_newest_profile(client, root):
    c = load_config(root=root)
    c.data_dir = root / "data"
    state = onboard.empty_state()
    state["draft"] = {"reader": "r", "profile": PROFILE, "notes": "n"}
    onboard.save_state(c, state)
    r = client.post(
        "/api/onboarding/edit", json={"reader": "new reader", "profile": PROFILE + "x"}, headers=H
    )
    assert r.json() == {"saved": "draft"}
    saved = onboard.load_state(c)["draft"]
    assert saved["reader"] == "new reader" and saved["notes"] == "n"


def test_email_password_goes_to_keyring_only(client, root, monkeypatch):
    stored = {}
    monkeypatch.setattr("radar.mailer.set_password", lambda u, p: stored.update({u: p}))
    r = client.post("/api/email/password", json={"password": "abcd efgh ijkl mnop"}, headers=H)
    assert r.status_code == 400  # no address yet
    from radar import settings

    settings.save(root, {"email.to": "me@gmail.com"})
    r = client.post("/api/email/password", json={"password": "abcd efgh ijkl mnop"}, headers=H)
    assert r.status_code == 200 and "abcd" not in r.text
    assert stored == {"me@gmail.com": "abcdefghijklmnop"}
    assert not any("abcd" in p.read_text(errors="ignore") for p in root.rglob("*") if p.is_file())
    assert (
        client.post("/api/email/password", json={"password": "abc"}, headers=H).status_code == 400
    )
    assert (
        client.post("/api/email/password", json={"password": "abcdefghijklmnop"}).status_code == 403
    )


def test_queue_csv_runs_and_digest_options(client, root):
    r = client.get("/api/queue.csv?all=true")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.text.startswith("rank,name,profile_url")
    assert not (root / "data" / "queue").exists()  # a download never rewrites the emailed files
    assert client.get("/api/runs").json() == []
    runs = root / "data" / "runs"
    runs.mkdir(parents=True)
    (runs / "2026-11-19T07-00-00.json").write_text(
        json.dumps({"started": "a", "ok": True, "stages": []})
    )
    (runs / "2026-11-20T07-00-00.json").write_text(
        json.dumps({"started": "b", "ok": False, "stages": []})
    )
    assert [r["started"] for r in client.get("/api/runs").json()] == ["b", "a"]
    client.post("/api/jobs", json={"action": "digest", "no_audio": True}, headers=H)
    assert client.runner.started[-1] == ("digest", ["--no-audio"])


def test_doctor_checks_name_panel_fixes(cfg):
    from radar.doctor import Probes, run_checks

    probes = Probes(
        which=lambda exe: None,
        http_get=lambda url: (_ for _ in ()).throw(OSError("down")),
        password=lambda user: None,
        task_exists=lambda name: False,
        playwright_dir=cfg.data_dir / "none",
    )
    actions = {c.name: c.action for c in run_checks(cfg, probes)}
    assert actions["ollama"].startswith("https://")
    assert actions["Playwright Chromium"] == "setup-chromium"
    assert actions["LinkedIn login"] == "login"
    assert actions["daily schedule"] == "schedule"
