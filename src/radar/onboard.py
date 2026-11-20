"""First-run onboarding: turn a CV or a short description into a field profile, check it
against real posts, and refine it from the user's corrections.

Steps (each one is a panel button; the slow ones run as `radar onboard <step>` jobs):
  1. upload   save the CV, extract its text, strip contact details
  2. draft    Codex or the local model writes a draft profile and reader line
  3. calibrate classify recent posts with the draft (live feed or stored posts; no DB writes)
  4. correct  the user flips the verdicts the draft got wrong
  5. refine   the model rewrites the profile from those corrections
  6. apply    back up the old profile, write the new one, save the reader line

State lives in data/onboarding/state.json so the wizard survives a closed tab or a restart.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Config
from .models import RawPost

CV_SUFFIXES = (".pdf", ".docx", ".txt", ".md")
CV_MAX_BYTES = 8 * 1024 * 1024
CV_MAX_CHARS = 40_000
LOCAL_CV_CHARS = 20_000  # the local model's context window is smaller
PROFILE_MAX_CHARS = 50_000
DRAFTERS = ("codex", "local")
SOURCES = ("live", "stored")

_S = {"type": "string"}
PROFILE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reader", "profile_markdown", "notes"],
    "properties": {"reader": _S, "profile_markdown": _S, "notes": _S},
}

SKELETON = """# Field profile

<one paragraph: who the reader is and what their work covers>

## Core (always save)

- <topic>: <named methods, models, tools, datasets, groups, or venues>

## Adjacent (save, lower priority)

- <topic>: <named examples>

## Skip

- Promoted or sponsored posts, LinkedIn News, ads.
- General AI business news (funding, stock prices, executive moves).
- <nearby fields that look relevant but are not, with examples>
- Personal milestones: graduations, PhD defenses, new jobs, internships, awards, congratulations,
  unless the post itself reports a concrete result, paper, dataset, or tool.

## What counts as worth saving

Informative progress: a new paper or preprint, dataset, model release, software tool, benchmark,
or substantive technical result or talk with enough content to learn from.
"""

DRAFT_TASK = """You are setting up a research-news filter for one person. It reads their LinkedIn
feed every day, saves posts about their field, and a stronger model later picks the 5 to 15 most
important items for a daily digest and podcast.

Write two things from the material below:

1. reader: one phrase saying who the digest is for, e.g. "a computational chemistry postdoc
   working on enzyme mechanisms with QM/MM". No names.
2. profile_markdown: a field profile that follows this skeleton exactly (same headings, in this
   order). Replace every <...> with real content.

{skeleton}

Rules for the profile:
- Be concrete. Name the methods, models, software, datasets, instruments, materials, organisms,
  research groups, facilities, and venues this person would care about. A filter matches named
  things far better than broad categories.
- Core: what their own research is about. Adjacent: methods and tools they use or follow, and
  neighboring areas worth a look. Skip: nearby fields that a keyword match would wrongly save,
  with examples, plus the standard skip lines in the skeleton.
- 4 to 8 bullets in Core, 3 to 6 in Adjacent, 4 to 7 in Skip.
- Write about the field, not the person's biography. Leave out names, employers' internal
  projects, and contact details.
- notes: two or three sentences on what you inferred and anything you were unsure about.

Material from the person (contact details already removed):

{material}
"""

REFINE_TASK = """You maintain a research-news filter for one reader. Below is the current field
profile and the result of testing it on real LinkedIn posts. The filter's verdict on each post
came from a small local model reading this profile. The reader corrected the verdicts that were
wrong.

Rewrite the profile so the filter would agree with the reader on every corrected post, without
breaking the posts the reader agreed with. Keep the same headings and order. Prefer adding
concrete named examples (methods, tools, datasets, groups, venues, nearby fields to skip) over
vague wording. Keep the reader line unless the corrections show it is wrong.

notes: list the changes you made, one short sentence each.

Current reader: {reader}

Current profile:
{profile}

Corrected posts (the reader disagreed with the filter):
{corrected}

Posts the reader agreed with:
{agreed}
"""


# ---- state ---------------------------------------------------------------------------------


def onboarding_dir(cfg: Config) -> Path:
    return cfg.data_dir / "onboarding"


def empty_state() -> dict[str, Any]:
    return {
        "cv": None,
        "notes": "",
        "drafter": "codex",
        "draft": None,
        "calibration": None,
        "corrections": {},
        "refined": None,
        "applied_at": None,
    }


def load_state(cfg: Config) -> dict[str, Any]:
    path = onboarding_dir(cfg) / "state.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return empty_state()
    return {**empty_state(), **saved}


def save_state(cfg: Config, state: dict[str, Any]) -> None:
    folder = onboarding_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, folder / "state.json")


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def current_profile(state: dict[str, Any]) -> dict[str, str] | None:
    """The newest profile the wizard has: refined if present, else the draft."""
    return state.get("refined") or state.get("draft")


# ---- step 1: CV ----------------------------------------------------------------------------


def extract_cv_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        text = "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    elif suffix == ".docx":
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
        xml = re.sub(r"</w:p>|<w:br/>|<w:tab/>", "\n", xml)
        text = html.unescape(re.sub(r"<[^>]+>", "", xml))
    elif suffix in (".txt", ".md"):
        text = path.read_text(encoding="utf-8", errors="replace")
    else:
        raise ValueError(f"unsupported file type {suffix}; use PDF, DOCX, TXT, or MD")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[:CV_MAX_CHARS]


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
# Not preceded by a word character, dot, slash, or colon: keeps DOIs (10.1016/j.x.2024.113410)
# and page ranges (23(17):10533-10544) intact.
_PHONE = re.compile(r"(?<![\w./:-])\+?\(?\d[\d\s().-]{8,}\d(?![\w/])")
_PROFILE_URL = re.compile(
    r"(https?://)?(www\.)?(linkedin\.com/in|github\.com|orcid\.org|twitter\.com|x\.com)/[\w./-]+",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """Remove contact details before any model sees the text: emails, phone numbers, and
    personal profile URLs. Year ranges such as 2019-2024 are kept (too few digits)."""
    text = _EMAIL.sub("[email]", text)
    text = _PROFILE_URL.sub("[profile link]", text)
    return _PHONE.sub(
        lambda m: "[phone]" if sum(c.isdigit() for c in m.group()) >= 10 else m.group(), text
    )


def save_cv(cfg: Config, filename: str, data: bytes) -> dict[str, Any]:
    """Extract and redact an uploaded CV. Returns the state's `cv` entry.

    Only the redacted text is kept (data/onboarding/cv.txt). The original file exists just
    long enough to be read and is always deleted, so no model can open it later. A file that
    fails validation leaves the previous CV in place."""
    suffix = Path(filename).suffix.lower()
    if suffix not in CV_SUFFIXES:
        raise ValueError(f"unsupported file type {suffix or '(none)'}; use PDF, DOCX, TXT, or MD")
    if len(data) > CV_MAX_BYTES:
        raise ValueError("file is larger than 8 MB")
    folder = onboarding_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    raw = folder / f"upload.tmp{suffix}"
    raw.write_bytes(data)
    try:
        text = redact(extract_cv_text(raw))
    except (ValueError, KeyError, zipfile.BadZipFile) as e:
        raise ValueError(f"could not read the file: {e}") from None
    except Exception as e:  # noqa: BLE001 - pypdf raises many types on damaged files
        raise ValueError(f"could not read the file: {e.__class__.__name__}") from None
    finally:
        raw.unlink(missing_ok=True)
    if len(text) < 200:
        raise ValueError(
            "found almost no text in the file (a scanned PDF?); paste a description instead"
        )
    tmp = folder / "cv.txt.tmp"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, folder / "cv.txt")
    return {"name": Path(filename).name[:120], "chars": len(text), "uploaded": _now()}


def remove_cv(cfg: Config) -> None:
    (onboarding_dir(cfg) / "cv.txt").unlink(missing_ok=True)


def material(cfg: Config, state: dict[str, Any], limit: int = CV_MAX_CHARS) -> str:
    parts = []
    cv = onboarding_dir(cfg) / "cv.txt"
    if state.get("cv") and cv.exists():
        parts.append("## CV\n\n" + cv.read_text(encoding="utf-8")[:limit])
    if state.get("notes", "").strip():
        parts.append("## In their own words\n\n" + redact(state["notes"].strip())[:8000])
    if not parts:
        raise ValueError("upload a CV or write a short description of your field first")
    return "\n\n".join(parts)


# ---- model backends ------------------------------------------------------------------------

JsonModel = Callable[[str, dict], dict]


def codex_model(cfg: Config, timeout_s: int = 1200) -> JsonModel:
    """Run one strict-schema Codex task. Its working folder holds only the schema and output,
    not the CV or wizard state: the task text carries everything Codex should see."""
    from .digest import codex_command, run_codex

    def call(task: str, schema: dict) -> dict:
        folder = onboarding_dir(cfg) / "codex"
        folder.mkdir(parents=True, exist_ok=True)
        schema_path, out_path = folder / "schema.json", folder / "codex_out.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        out_path.unlink(missing_ok=True)
        run_codex(codex_command(cfg, folder, schema_path, out_path), task, timeout_s)
        return json.loads(out_path.read_text(encoding="utf-8"))

    return call


def local_model(cfg: Config) -> JsonModel:
    from .llm import OllamaModel

    m = OllamaModel(
        cfg.llm.host, cfg.llm.model, max(cfg.llm.num_ctx, 16384), max(cfg.llm.timeout_s, 600)
    )
    system = "You write precise research-filter profiles. Return only the JSON object."
    return lambda task, schema: m.json_chat(system, task, schema)


def _clean_profile(result: dict) -> dict[str, str]:
    reader = " ".join(str(result.get("reader", "")).split())[:300]
    profile = str(result.get("profile_markdown", "")).replace("\r\n", "\n").strip()
    if not reader or "## Core" not in profile or "## Skip" not in profile:
        raise RuntimeError("the model returned an incomplete profile; try again")
    return {
        "reader": reader,
        "profile": profile[:PROFILE_MAX_CHARS] + "\n",
        "notes": str(result.get("notes", "")).strip()[:2000],
    }


# ---- step 2: draft -------------------------------------------------------------------------


def draft_profile(cfg: Config, model: JsonModel | None = None, log=print) -> dict[str, str]:
    state = load_state(cfg)
    drafter = state.get("drafter") if state.get("drafter") in DRAFTERS else "codex"
    limit = LOCAL_CV_CHARS if drafter == "local" else CV_MAX_CHARS
    task = DRAFT_TASK.format(skeleton=SKELETON, material=material(cfg, state, limit))
    model = model or (local_model(cfg) if drafter == "local" else codex_model(cfg))
    log(f"drafting the field profile with {'the local model' if drafter == 'local' else 'Codex'}")
    draft = _clean_profile(model(task, PROFILE_SCHEMA)) | {"by": drafter, "at": _now()}
    state = load_state(cfg)  # re-read: the user may have edited notes meanwhile
    state.update(draft=draft, refined=None, calibration=None, corrections={}, applied_at=None)
    save_state(cfg, state)
    log(f"draft ready: {draft['reader']}")
    return draft


# ---- step 3: calibrate ---------------------------------------------------------------------


def _post_card(i: int, post: RawPost, verdict) -> dict[str, Any]:
    return {
        "id": i,
        "author": post.author or "unknown",
        "headline": (post.headline or "")[:160],
        "text": post.text[:700],
        "links": post.links[:3],
        "tier": verdict.tier,
        "summary": verdict.summary,
        "reason": verdict.reason,
    }


def calibrate(
    cfg: Config,
    posts: list[RawPost],
    classify: Callable[[RawPost], Any],
    source: str,
    log=print,
) -> dict[str, Any]:
    """Classify posts with the draft profile and keep the verdicts for review. Never writes
    to the posts database."""
    cards, errors = [], 0
    for post in posts:
        if post.promoted or len(post.text) < 40:
            continue
        try:
            verdict = classify(post)
        except Exception as e:  # noqa: BLE001 - one bad post should not end calibration
            errors += 1
            log(f"  could not judge a post by {post.author}: {e}")
            continue
        cards.append(_post_card(len(cards), post, verdict))
        mark = "keep" if verdict.tier != "skip" else "skip"
        log(f"  {mark:4} {post.author}: {verdict.summary[:90]}")
    if not cards:
        raise RuntimeError("no posts to calibrate on; scroll the feed or try the other source")
    result = {"source": source, "at": _now(), "posts": cards, "errors": errors}
    state = load_state(cfg)
    if state.get("refined"):
        # Testing a refined profile: it becomes the base, and the wizard goes back to step 3
        # with the new calls instead of offering the old refinement.
        state["draft"] = state["refined"]
        state["refined"] = None
    state.update(calibration=result, corrections={})
    save_state(cfg, state)
    kept = sum(c["tier"] != "skip" for c in cards)
    log(f"calibration: {kept} of {len(cards)} posts would be saved")
    return result


def set_corrections(cfg: Config, corrections: dict[str, str]) -> dict[str, str]:
    """Record the user's verdicts: post id -> "keep" | "skip". Only flips are stored."""
    state = load_state(cfg)
    cal = state.get("calibration") or {}
    by_id = {str(c["id"]): c for c in cal.get("posts", [])}
    clean = {}
    for pid, want in corrections.items():
        card = by_id.get(str(pid))
        if card is None or want not in ("keep", "skip"):
            continue
        model_keeps = card["tier"] != "skip"
        if (want == "keep") != model_keeps:
            clean[str(pid)] = want
    state["corrections"] = clean
    save_state(cfg, state)
    return clean


# ---- step 5: refine ------------------------------------------------------------------------


def _post_line(card: dict, verdict: str) -> str:
    text = " ".join(card["text"].split())[:400]
    return f"- [{verdict}] {card['author']} ({card['headline'][:80]}): {text}"


def refine_profile(cfg: Config, model: JsonModel | None = None, log=print) -> dict[str, str]:
    state = load_state(cfg)
    base = current_profile(state)
    cal = state.get("calibration")
    if not base or not cal:
        raise ValueError("draft and calibrate first")
    corr = state.get("corrections") or {}
    corrected, agreed = [], []
    for card in cal["posts"]:
        model_says = "keep" if card["tier"] != "skip" else "skip"
        if str(card["id"]) in corr:
            corrected.append(
                _post_line(card, f"filter said {model_says}, reader says {corr[str(card['id'])]}")
            )
        else:
            agreed.append(_post_line(card, model_says))
    if not corrected:
        raise ValueError("no corrections recorded; the profile already matches your calls")
    task = REFINE_TASK.format(
        reader=base["reader"],
        profile=base["profile"],
        corrected="\n".join(corrected),
        agreed="\n".join(agreed[:25]) or "(none)",
    )
    drafter = state.get("drafter") if state.get("drafter") in DRAFTERS else "codex"
    model = model or (local_model(cfg) if drafter == "local" else codex_model(cfg))
    log(f"refining the profile from {len(corrected)} corrections")
    refined = _clean_profile(model(task, PROFILE_SCHEMA)) | {"by": drafter, "at": _now()}
    state = load_state(cfg)
    state["refined"] = refined
    save_state(cfg, state)
    log("refined profile ready")
    return refined


# ---- step 6: apply -------------------------------------------------------------------------


def apply_profile(cfg: Config, reader: str, profile: str) -> Path | None:
    """Write the approved profile and reader line. The old profile is copied to
    data/onboarding/backups first. Returns the backup path (None if there was no profile)."""
    from . import settings

    reader = " ".join(reader.split())
    profile = profile.replace("\r\n", "\n").strip() + "\n"
    if not reader or not profile.strip():
        raise ValueError("reader and profile are both required")
    if len(profile) > PROFILE_MAX_CHARS:
        raise ValueError("profile is too long")
    settings.validate({"profile.reader": reader}, {})
    backup = None
    if cfg.field_profile.exists():
        backups = onboarding_dir(cfg) / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        backup = backups / f"field_profile-{dt.datetime.now():%Y%m%d-%H%M%S}.md"
        backup.write_text(cfg.field_profile.read_text(encoding="utf-8"), encoding="utf-8")
    cfg.field_profile.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.field_profile.with_suffix(".md.tmp")
    tmp.write_text(profile, encoding="utf-8")
    os.replace(tmp, cfg.field_profile)
    settings.save(cfg.root, {"profile.reader": reader})
    # The applied text now lives in config/field_profile.md, where it can be edited further.
    # Clear the drafts so a later visit cannot re-apply a stale one over those edits.
    state = load_state(cfg)
    state.update(applied_at=_now(), draft=None, refined=None, calibration=None, corrections={})
    save_state(cfg, state)
    return backup
