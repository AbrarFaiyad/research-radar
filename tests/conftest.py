import json
import smtplib
from pathlib import Path

import pytest

from radar.config import BuildsCfg, EmailCfg, NotebookLMCfg, PodcastsCfg, load_config
from radar.db import Store

FIXTURES = Path(__file__).parent / "fixtures"

# Verdicts a well-behaved model would give for the synthetic feed, keyed by a text snippet.
EXPECTED = {
    "molten LiCl-KCl": {
        "tier": "core",
        "kind": "preprint",
        "topics": ["mlip", "mace", "molten salts"],
        "summary": "LoRA fine-tuning of MACE for molten LiCl-KCl.",
        "reason": "core MLIP",
    },
    "10,000 GPUs": {
        "tier": "adjacent",
        "kind": "paper",
        "topics": ["hpc", "mlip"],
        "summary": "Scaling equivariant GNN potentials on exascale.",
        "reason": "HPC",
    },
    "defended today": {
        "tier": "skip",
        "kind": "milestone",
        "topics": [],
        "summary": "PhD defense congratulations.",
        "reason": "milestone",
    },
    "capacity auction": {
        "tier": "skip",
        "kind": "news",
        "topics": [],
        "summary": "Energy market auction.",
        "reason": "unrelated",
    },
    "oxygen substitution in MoS2": {
        "tier": "core",
        "kind": "paper",
        "topics": ["mos2", "tribology"],
        "summary": "ML-potential MD of O-doped MoS2 friction.",
        "reason": "core",
    },
    "50 million DFT": {
        "tier": "core",
        "kind": "dataset",
        "topics": ["dataset", "electrochemistry"],
        "summary": "Open DFT dataset for electrochemical interfaces.",
        "reason": "core",
    },
}


class FakeModel:
    """Stands in for Ollama. Classifies by snippet; records calls."""

    def __init__(self, curate_items=None):
        self.calls = []
        self.curate_items = curate_items

    def json_chat(self, system, user, schema):
        self.calls.append((system, user))
        if "items" in schema.get("properties", {}):
            if self.curate_items is not None:
                return {"items": self.curate_items}
            views = json.loads(user)
            return {
                "items": [
                    {"post_id": v["post_id"], "title": v["summary"], "why": "test"} for v in views
                ]
            }
        for snippet, verdict in EXPECTED.items():
            if snippet in user:
                return dict(verdict)
        return {"tier": "skip", "kind": "other", "topics": [], "summary": "", "reason": "no match"}


@pytest.fixture
def fake_model():
    return FakeModel()


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _build_data_in_tmp(tmp_path, monkeypatch):
    """The default build data folder (user app data) points into the test's temp folder."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


@pytest.fixture(autouse=True)
def _no_real_mail(monkeypatch):
    """No test may open an SMTP connection."""

    def refuse(*a, **k):
        raise AssertionError("tests must not send email")

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    monkeypatch.setattr(smtplib, "SMTP_SSL", refuse)


@pytest.fixture
def cfg(tmp_path):
    c = load_config(root=Path(__file__).resolve().parents[1])
    c.data_dir = tmp_path / "data"
    # never the owner's real builds settings (config.toml may enable them for the real repo)
    c.builds = BuildsCfg(work_dir=str(tmp_path / "builds"))
    # same for podcast publishing: disabled, never the owner's repo
    c.podcasts = PodcastsCfg()
    # never the owner's real email or NotebookLM settings
    c.email = EmailCfg()
    c.notebooklm = NotebookLMCfg(enabled=False)
    return c


@pytest.fixture
def feed_records():
    return json.loads((FIXTURES / "feed_sample.json").read_text(encoding="utf-8"))
