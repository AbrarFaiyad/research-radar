import datetime as dt
import json

from radar.classify import Classifier
from radar.digest import CODEX_SCHEMA, codex_command, curate, render_markdown, run_digest
from radar.podcast import clean_lines
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


def test_curate_ignores_unknown_ids_and_duplicates(store):
    _seed(store)
    rows = store.saved_posts(undigested_only=True)
    first = rows[0]["id"]
    model = FakeModel(curate_items=[
        {"post_id": first, "title": "A", "why": "w"},
        {"post_id": first, "title": "A again", "why": "dup"},
        {"post_id": 99999, "title": "ghost", "why": "made up"},
    ])
    items = curate(rows, model, "p", k=8, today=TODAY)
    assert [i.post_id for i in items] == [first]


def test_curate_falls_back_without_model(store):
    _seed(store)
    rows = store.saved_posts(undigested_only=True)
    items = curate(rows, None, "p", k=2)
    assert len(items) == 2
    tiers = {r["id"]: r["tier"] for r in rows}
    assert all(tiers[i.post_id] == "core" for i in items)


FAKE_RESULT = {
    "headline": "LoRA makes foundation potentials cheap to specialize",
    "overview": "Two items.",
    "papers": [{
        "title": "Low-rank adaptation of MACE for molten salts", "authors": "D. Example et al.",
        "venue": "preprint", "url": "https://example.org/preprint/molten-salt-lora",
        "what": "w", "how": "h", "why_it_matters": "y", "relevance": "r", "limitations": "l",
        "confidence": "high",
    }],
    "podcast": [{"speaker": "A", "text": "Welcome."}, {"speaker": "B", "text": "Why LoRA?"}],
}


def test_run_digest_end_to_end_with_fakes(cfg, store):
    _seed(store)
    seen = {}

    def runner(cmd, task):
        seen["cmd"], seen["task"] = cmd, task
        out = cmd[cmd.index("-o") + 1]
        with open(out, "w", encoding="utf-8") as f:
            json.dump(FAKE_RESULT, f)

    def synth(lines, out_wav):
        seen["lines"] = lines
        out_wav.write_bytes(b"RIFF")
        return out_wav

    res = run_digest(cfg, store, FakeModel(), runner=runner, synthesize=synth,
                     log=lambda s: None, today=TODAY)
    assert res is not None and len(res.items) == 4
    assert "molten LiCl-KCl" in seen["task"] and "2026-11-19" in seen["task"]
    md = res.summary_md.read_text(encoding="utf-8")
    assert "LoRA makes foundation potentials" in md and "**How.** h" in md
    assert res.audio and res.audio.exists()
    assert seen["lines"][1]["text"] == "Why LoRA?"
    # Everything saved is now marked as digested; a second run has nothing to do.
    assert store.saved_posts(undigested_only=True) == []
    assert run_digest(cfg, store, FakeModel(), runner=runner, log=lambda s: None) is None


def test_render_markdown_lists_selection():
    from radar.models import DigestItem
    md = render_markdown(FAKE_RESULT, "2026-11-19", [DigestItem(1, "T", [], "why")])
    assert md.startswith("# Research digest, 2026-11-19")
    assert "- T (why)" in md
    missing = dict(FAKE_RESULT, papers=[dict(FAKE_RESULT["papers"][0], url="")])
    assert "Primary source not found." in render_markdown(missing, "d", [])
    assert "<>" not in render_markdown(missing, "d", [])


def test_clean_lines_normalizes_speakers_and_markup():
    out = clean_lines([{"speaker": "A", "text": "**Hi**  there"}, {"speaker": "x", "text": "ok"},
                       {"speaker": "A", "text": "   "}])
    assert out == [{"speaker": "A", "text": "Hi there"}, {"speaker": "B", "text": "ok"}]
