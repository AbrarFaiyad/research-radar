import asyncio
import csv
import random

import pytest

from radar.browser.mcp_client import BrowserMCP, ToolNotAllowed, parse_evaluate_result
from radar.classify import Classifier, coerce_verdict, prefilter
from radar.config import ScanCfg
from radar.models import RawPost
from radar.queue import queue_records, write_queue
from radar.scanner import SCROLL_JS, collect_feed, process_posts, scan_file


def test_prefilter_skips_ads_and_empty():
    ad = RawPost.from_dict(
        {"key": "a", "text": "Buy now, big savings on everything!!", "promoted": True}
    )
    assert prefilter(ad).kind == "ad"
    empty = RawPost.from_dict({"key": "b", "text": "Nice!"})
    assert prefilter(empty).tier == "skip"
    real = RawPost.from_dict({"key": "c", "text": "x" * 100})
    assert prefilter(real) is None


def test_coerce_verdict_rejects_bad_enums():
    v = coerce_verdict({"tier": "maybe", "kind": "blog", "topics": ["MLIP", "", "x"] * 3})
    assert v.tier == "skip" and v.kind == "other"
    assert v.topics == ["mlip", "x", "mlip", "x", "mlip"]
    assert coerce_verdict({"topics": ["moly\u200bbdenum  Salts"]}).topics == ["molybdenum salts"]


def test_classifier_includes_profile_and_date(fake_model):
    import datetime as dt

    c = Classifier(fake_model, "PROFILE-TEXT", today=dt.date(2026, 11, 19))
    assert "2026-11-19" in c.system and "PROFILE-TEXT" in c.system


def test_scan_file_classifies_dedupes_and_builds_queue(cfg, store, fake_model):
    from tests.conftest import FIXTURES

    classify = Classifier(fake_model, cfg.field_profile_text())
    rep = scan_file(FIXTURES / "feed_sample.json", store, classify, log=lambda s: None)
    # 8 cards; the last repeats the first card's text under a new key, dropped at collection.
    assert rep.seen == 7
    assert rep.duplicates == 0
    assert rep.saved == 4  # molten salt, HPC, MoS2 (1st degree), dataset (company)
    assert rep.skipped == 3  # ad, milestone, unrelated
    assert store.stats() == {"core": 3, "adjacent": 1, "skip": 3}
    # The ad never reached the model.
    assert not any("Unlimited storage" in u for _, u in fake_model.calls)

    names = [r["name"] for r in queue_records(store)]
    # 1st-degree author, company page, and authors of skipped posts are excluded.
    assert names == ["Dana Example", "Lee Placeholder"]


def test_queue_mark_and_export(cfg, store, fake_model, tmp_path):
    from tests.conftest import FIXTURES

    scan_file(FIXTURES / "feed_sample.json", store, Classifier(fake_model, "p"), log=lambda s: None)
    assert store.set_author_status("https://www.linkedin.com/in/dana-example-000", "sent") == 1
    md, csv_path, n = write_queue(store, tmp_path)
    assert n == 1
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert rows[0]["name"] == "Lee Placeholder" and rows[0]["degree"] == "3rd+"
    text = md.read_text(encoding="utf-8")
    assert "[Lee Placeholder](https://www.linkedin.com/in/lee-placeholder-000/)" in text
    assert "Dana Example" not in text
    _, _, n_all = write_queue(store, tmp_path, include_done=True)
    assert n_all == 2


def test_resighting_updates_degree_and_kind(store, fake_model):
    from tests.conftest import FIXTURES

    scan_file(FIXTURES / "feed_sample.json", store, Classifier(fake_model, "p"), log=lambda s: None)
    assert "Dana Example" in [r["name"] for r in queue_records(store)]
    # Same post seen again after the user connected: author leaves the queue.
    again = RawPost.from_dict(
        {
            "key": "new-key",
            "author": "Dana Example",
            "authorUrl": "https://www.linkedin.com/in/dana-example-000/",
            "authorKind": "unknown",
            "degree": "1st",
            "text": json_text_of(FIXTURES, "k-core-mlip"),
        }
    )
    rep = process_posts(
        [again], store, Classifier(fake_model, "p"), resolve=lambda u, t: [], log=lambda s: None
    )
    assert rep.duplicates == 1
    assert "Dana Example" not in [r["name"] for r in queue_records(store)]
    kind = store.conn.execute("SELECT kind FROM authors WHERE name='Dana Example'").fetchone()[0]
    assert kind == "person"  # 'unknown' never overwrites


def json_text_of(fixtures, key):
    import json

    recs = json.loads((fixtures / "feed_sample.json").read_text(encoding="utf-8"))
    return next(r["text"] for r in recs if r["key"] == key)


def test_reclassify_upgrades_and_resolves_links(store, fake_model):
    from radar.scanner import reclassify_stored
    from tests.conftest import EXPECTED

    post = RawPost.from_dict(
        {
            "key": "r",
            "author": "Lee Placeholder",
            "authorKind": "person",
            "authorUrl": "https://www.linkedin.com/in/lee-placeholder-000/",
            "degree": "2nd",
            "text": "Our SC paper on 10,000 GPUs " + "x" * 50,
            "links": ["https://lnkd.in/abc"],
        }
    )
    process_posts(
        [post], store, Classifier(_Fixed("skip"), "p"), resolve=lambda u, t: [], log=lambda s: None
    )
    assert store.stats() == {"skip": 1}
    seen = {}

    def resolve(urls, text):
        seen["urls"] = urls
        return [{"url": "https://arxiv.org/abs/2611.1", "kind": "arxiv"}]

    checked, changed = reclassify_stored(
        store, Classifier(fake_model, "p"), resolve=resolve, log=lambda s: None
    )
    assert (checked, changed) == (1, 1)
    assert store.stats() == {EXPECTED["10,000 GPUs"]["tier"]: 1}
    assert seen["urls"] == ["https://lnkd.in/abc"]
    assert [r["name"] for r in queue_records(store)] == ["Lee Placeholder"]


class _Fixed:
    def __init__(self, tier):
        self.tier = tier

    def json_chat(self, system, user, schema):
        return {"tier": self.tier, "kind": "other", "topics": [], "summary": "s", "reason": "r"}


def test_collector_dedupes_same_text_under_new_key():
    from radar.scanner import Collector

    c = Collector()
    assert c.add([{"key": "a", "author": "X", "text": "same words here"}]) == 1
    assert c.add([{"key": "b", "author": "X", "text": "same  words here"}]) == 0


def test_process_posts_records_model_errors(store):
    class Boom:
        def json_chat(self, *a):
            raise TimeoutError("ollama slow")

    post = RawPost.from_dict({"key": "x", "author": "A", "text": "y" * 200})
    rep = process_posts(
        [post], store, Classifier(Boom(), "p"), resolve=lambda u, t: [], log=lambda s: None
    )
    assert rep.errors and rep.saved == 0
    assert not store.has_post(post)  # will be retried next scan


def _run(coro):
    return asyncio.run(coro)


def test_collect_feed_dedupes_and_stops_when_stale(feed_records):
    pages = [feed_records[:3], feed_records[2:5], feed_records[4:8]]
    calls = {"extract": 0, "scroll": 0, "wait": []}

    async def evaluate(js):
        if js.startswith("() => {\n  const px"):
            calls["scroll"] += 1
            return {}
        i = calls["extract"]
        calls["extract"] += 1
        return pages[i] if i < len(pages) else pages[-1]

    async def wait(s):
        calls["wait"].append(s)

    cfg = ScanCfg(
        max_posts=100, max_minutes=60, stale_steps_stop=2, pause_s_min=2.5, pause_s_max=6.0
    )
    posts = _run(collect_feed(evaluate, wait, cfg, rng=random.Random(0)))
    assert [p.key for p in posts] == [
        r["key"] for r in feed_records if r["key"] != "k-core-mlip-dup"
    ]
    assert calls["extract"] == 5  # 3 productive pages + 2 stale
    assert all(2.5 <= w <= 6.0 for w in calls["wait"])


def test_collect_feed_respects_max_posts(feed_records):
    async def evaluate(js):
        return feed_records

    async def wait(s):
        pass

    posts = _run(collect_feed(evaluate, wait, ScanCfg(max_posts=3), rng=random.Random(0)))
    assert len(posts) == 3


def test_scroll_js_targets_workspace():
    js = SCROLL_JS % 700
    assert "main#workspace" in js and "700" in js


def test_parse_evaluate_result_shapes():
    playwright_mcp = '### Result\n[\n  {"a": 1}\n]\n### Ran Playwright code\n```js\nawait page.evaluate(...)\n```'
    assert parse_evaluate_result(playwright_mcp) == [{"a": 1}]
    assert parse_evaluate_result('{"b": 2}') == {"b": 2}
    assert parse_evaluate_result('```json\n{"c": 3}\n```') == {"c": 3}


def test_allowlist_blocks_click_tools(tmp_path):
    b = BrowserMCP("npx", [], tmp_path, tmp_path)
    for tool in ("browser_click", "browser_type", "browser_run_code_unsafe", "browser_fill_form"):
        with pytest.raises(ToolNotAllowed):
            _run(b.call(tool, {}))


def test_scan_stage_fails_when_every_post_fails(cfg, monkeypatch):
    from radar import cli

    class Rep:
        seen, new, saved, skipped, errors = 3, 3, 0, 0, ["classify A: timeout"] * 3

        def line(self):
            return "x"

    monkeypatch.setattr(cli, "_model", lambda c: None)
    monkeypatch.setattr("radar.scanner.scan_file", lambda *a, **k: Rep())
    with pytest.raises(RuntimeError, match="all 3 new posts failed"):
        cli.stage_scan(cfg, from_file="x.json")
