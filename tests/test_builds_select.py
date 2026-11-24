import pytest

from radar.builds.select import History, HistoryCorrupt, folder_name, package_slug, pick

NO_ITEM = "no item with public code, an open license, and a CPU demo"


def item(title, code_url="", license_="", cpu=False, **extra):
    return {
        "title": title,
        "kind": "paper",
        "code_url": code_url,
        "license": license_,
        "cpu_demo": cpu,
        **extra,
    }


def result_with(items):
    return {"items": items}


paper_no_code = item("Paper A")
unlicensed_repo = item("B", "https://github.com/o/b", "", True)
mit_repo_cpu = item("MACE", "https://github.com/ACEsuit/mace-torch", "MIT", True)
apache_repo_cpu = item("C", "https://github.com/o/c", "Apache-2.0", True)


def test_pick_first_eligible_in_rank_order():
    c, _ = pick(result_with([paper_no_code, unlicensed_repo, mit_repo_cpu, apache_repo_cpu]), set())
    assert c.rank == 3 and c.license == "MIT" and c.package == "mace-torch"


def test_skip_reasons():
    assert pick(result_with([paper_no_code]), set()) == (None, NO_ITEM)
    assert pick({"items": [{"title": "old"}]}, set())[0] is None
    assert pick(result_with([mit_repo_cpu]), {"mace-torch"})[0] is None


def test_built_item_falls_through_to_next():
    c, _ = pick(result_with([mit_repo_cpu, apache_repo_cpu]), {"mace-torch"})
    assert c.rank == 2


def test_candidate_item_is_whitelisted():
    c, _ = pick(
        result_with([mit_repo_cpu | {"relevance": "my private work", "post_ids": [3]}]), set()
    )
    assert "relevance" not in c.item and "post_ids" not in c.item
    assert c.item["title"] == "MACE" and c.item["license"] == "MIT"


def test_compound_or_unknown_license_skipped():
    for lic in ("MIT OR GPL-3.0", "", "Custom"):
        assert pick(result_with([item("X", "https://github.com/o/x", lic, True)]), set())[0] is None


def test_cpu_demo_required():
    assert pick(result_with([item("X", "https://github.com/o/x", "MIT", False)]), set())[0] is None


def test_package_slug():
    assert package_slug("https://github.com/o/Some_Repo.git", "t") == "some-repo"
    assert package_slug("https://pypi.org/project/Foo-Bar/", "t") == "foo-bar"
    assert package_slug("https://example.org/x", "Cool Title: Part 2!") == "cool-title-part-2"
    assert len(package_slug("", "a" * 100)) == 40


def test_folder_name_format():
    assert (
        folder_name("2026-11-21", "mace-torch", "LoRA fine-tune: molten salt!")
        == "2026-11-21_mace-torch_lora-fine-tune-molten-salt"
    )


def test_history_counts_only_published(tmp_path):
    h = History(tmp_path / "sub" / "history.json")
    assert h.built() == set() and not h.published_on("2026-11-21")
    h.add_attempt("a", "failed", "boom")
    assert h.built() == set()
    h.add_published("b", "2026-11-21_b_task", "https://github.com/me/r/tree/main/x")
    assert h.built() == {"b"}
    assert h.published_on("2026-11-21") and not h.published_on("2026-11-22")
    assert History(tmp_path / "sub" / "history.json").built() == {"b"}


@pytest.mark.parametrize("day", ["../x", "a/b", "2026-11-2", "", "2026-11-21/"])
def test_folder_name_rejects_bad_day(day):
    with pytest.raises(ValueError):
        folder_name(day, "p", "t")


def test_corrupt_history_raises_and_is_untouched(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("{not json", encoding="utf-8")
    h = History(path)
    for call in (h.built, lambda: h.add_attempt("a", "failed", "x"), lambda: h.published_on("d")):
        with pytest.raises(HistoryCorrupt):
            call()
    assert path.read_bytes() == b"{not json"
    assert list(tmp_path.iterdir()) == [path]


def test_folder_name_falls_back_for_empty_slugs():
    assert folder_name("2026-11-21", "", "") == "2026-11-21_item_demo"
    assert folder_name("2026-11-21", "\u6f22\u5b57", "\u00e9\u00e8!!") == "2026-11-21_item_demo"
    assert folder_name("2026-11-21", "mace", "\u6f22") == "2026-11-21_mace_demo"
