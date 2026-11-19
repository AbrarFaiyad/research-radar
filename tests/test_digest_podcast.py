import datetime as dt
import json

from radar.classify import Classifier
from radar.digest import (
    CODEX_SCHEMA,
    build_codex_task,
    codex_command,
    render_markdown,
    run_digest,
    source_urls,
)
from radar.scanner import scan_file
from tests.conftest import FIXTURES, FakeModel

TODAY = dt.date(2026, 11, 19)


def _seed(store):
    scan_file(FIXTURES / "feed_sample.json", store, Classifier(FakeModel(), "p"), log=lambda s: None)


def _walk(schema):
    yield schema
    for v in schema.get("properties", {}).values():
        yield from _walk(v)
    if "items" in schema:
        yield from _walk(schema["items"])


def test_codex_schema_is_strict():
    # Codex structured output requires every object to be closed and fully required.
    for node in _walk(CODEX_SCHEMA):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])


def test_codex_command_puts_search_before_exec(cfg, tmp_path):
    cmd = codex_command(cfg, tmp_path, tmp_path / "s.json", tmp_path / "o.json")
    assert cmd.index("--search") < cmd.index("exec")
    assert cmd[cmd.index("-s") + 1] == "read-only"
    assert cmd[-1] == "-"
    cfg.digest.web_search = False
    assert "--search" not in codex_command(cfg, tmp_path, tmp_path / "s.json", tmp_path / "o.json")


def test_task_sends_every_candidate_with_limits(store, cfg):
    _seed(store)
    rows = store.saved_posts(undigested_only=True)
    template = (cfg.root / "prompts" / "codex_digest.md").read_text(encoding="utf-8")
    task = build_codex_task(rows, "PROFILE", template, 5, 15, today=TODAY)
    assert all(f"### Candidate {r['id']}" in task for r in rows)
    assert "between 5 and 15" in task and "2026-11-19" in task and "PROFILE" in task


def test_braces_in_post_text_do_not_break_formatting(store, cfg):
    from radar.models import RawPost, Verdict
    post = RawPost.from_dict({"key": "b", "author": "A", "text": "Energy E = {x | x > 0} " * 3})
    store.add_post(post, Verdict("core", "paper", [], "s", "r"), [])
    template = (cfg.root / "prompts" / "codex_digest.md").read_text(encoding="utf-8")
    task = build_codex_task(store.saved_posts(), "p", template, 5, 15)
    assert "{x | x > 0}" in task


FAKE_RESULT = {
    "headline": "LoRA makes foundation potentials cheap to specialize",
    "overview": "Reviewed 4, kept 1.",
    "items": [{
        "post_ids": [1], "title": "Low-rank adaptation of MACE for molten salts",
        "kind": "preprint", "authors": "D. Example et al.", "venue": "arXiv",
        "primary_url": "https://example.org/preprint/molten-salt-lora",
        "sources": [{"title": "Code", "url": "https://github.com/example/lora"},
                    {"title": "Dup", "url": "https://example.org/preprint/molten-salt-lora"}],
        "what": "w", "how": "h", "why_it_matters": "y", "relevance": "r", "limitations": "l",
        "impact": "high", "confidence": "high",
    }],
    "rejected": [{"post_id": 2, "reason": "HPC, low signal"}],
    "podcast_focus": "Lead with LoRA.",
}


def test_run_digest_sends_all_candidates_and_marks_them(cfg, store):
    _seed(store)
    n_saved = len(store.saved_posts(undigested_only=True))
    seen = {}

    def runner(cmd, task):
        seen["task"] = task
        with open(cmd[cmd.index("-o") + 1], "w", encoding="utf-8") as f:
            json.dump(FAKE_RESULT, f)

    def make_audio(folder, result):
        seen["audio_called_with"] = result["podcast_focus"]
        out = folder / "episode.m4a"
        out.write_bytes(b"x")
        return out

    res = run_digest(cfg, store, runner=runner, make_audio=make_audio, log=lambda s: None,
                     today=TODAY)
    assert res is not None and res.reviewed == n_saved == 4 and res.kept == 1
    assert seen["task"].count("### Candidate ") == 4
    md = res.summary_md.read_text(encoding="utf-8")
    assert "1 kept of 4 candidate posts" in md and "[Code](https://github.com/example/lora)" in md
    assert (res.folder / "sources.txt").read_text(encoding="utf-8").split() == [
        "https://example.org/preprint/molten-salt-lora", "https://github.com/example/lora"]
    assert seen["audio_called_with"] == "Lead with LoRA." and res.audio.exists()
    # Codex judged all four, so none come back tomorrow.
    assert store.saved_posts(undigested_only=True) == []


def test_audio_failure_keeps_digest(cfg, store):
    _seed(store)

    def runner(cmd, task):
        with open(cmd[cmd.index("-o") + 1], "w", encoding="utf-8") as f:
            json.dump(FAKE_RESULT, f)

    def broken(folder, result):
        raise RuntimeError("NotebookLM UI changed")

    logs = []
    res = run_digest(cfg, store, runner=runner, make_audio=broken, log=logs.append, today=TODAY)
    assert res.summary_md.exists() and res.audio is None
    assert any("radar podcast" in line for line in logs)


def test_same_day_reruns_get_new_folders(cfg, store):
    _seed(store)

    def runner(cmd, task):
        with open(cmd[cmd.index("-o") + 1], "w", encoding="utf-8") as f:
            json.dump(FAKE_RESULT, f)

    first = run_digest(cfg, store, runner=runner, log=lambda s: None, today=TODAY)
    from radar.models import RawPost, Verdict
    store.add_post(RawPost.from_dict({"key": "n", "author": "B", "text": "new post " * 10}),
                   Verdict("core", "paper", [], "s", "r"), [])
    second = run_digest(cfg, store, runner=runner, log=lambda s: None, today=TODAY)
    assert (first.folder.name, second.folder.name) == ("2026-11-19", "2026-11-19-2")


def test_render_and_sources_handle_missing_urls():
    missing = dict(FAKE_RESULT, items=[dict(FAKE_RESULT["items"][0], primary_url="", sources=[])])
    md = render_markdown(missing, "d", 3)
    assert "Primary source not found." in md and "<>" not in md
    assert source_urls(missing) == []
