import datetime as dt
import wave
from typing import ClassVar

import pytest

from radar import mailer
from radar.config import EmailCfg
from radar.podcast import wav_to_mp3

CFG = EmailCfg(enabled=True, to="me@example.org")


def _digest(tmp_path):
    d = tmp_path / "digests" / "2026-11-19"
    d.mkdir(parents=True)
    (d / "summary.md").write_text(
        "# Research digest\n\n**Headline**\n\n| a | b |\n|---|---|\n| 1 | 2 |\n", encoding="utf-8"
    )
    (d / "podcast_script.md").write_text("**A:** hi", encoding="utf-8")
    (d / "episode.mp3").write_bytes(b"ID3" + b"\0" * 100)
    q = tmp_path / "queue"
    q.mkdir()
    (q / "connect_queue.md").write_text(
        "# Connection queue\n\n| # | Name |\n|---|---|\n| 1 | X |\n", encoding="utf-8"
    )
    (q / "connect_queue.csv").write_text("rank,name\n1,X\n", encoding="utf-8")
    return d, q


def test_compose_daily_body_and_attachments(tmp_path):
    d, q = _digest(tmp_path)
    msg = mailer.compose_daily(CFG, d, q, today=dt.date(2026, 11, 19))
    assert msg["Subject"] == "Research Radar 2026-11-19"
    assert msg["To"] == msg["From"] == "me@example.org"
    names = [p.get_filename() for p in msg.iter_attachments()]
    assert names == ["episode.mp3", "summary.md", "connect_queue.csv"]
    html = msg.get_body(("html",)).get_content()
    assert "<table>" in html and "Connection queue" in html


def test_compose_without_digest_still_sends_queue(tmp_path):
    _, q = _digest(tmp_path)
    msg = mailer.compose_daily(CFG, None, q, today=dt.date(2026, 11, 19))
    assert "No new saved posts" in msg.get_body(("plain",)).get_content()
    assert [p.get_filename() for p in msg.iter_attachments()] == ["connect_queue.csv"]


def test_oversize_files_are_listed_not_attached(tmp_path):
    big, small = tmp_path / "big.wav", tmp_path / "small.md"
    big.write_bytes(b"0" * 200)
    small.write_bytes(b"0" * 10)
    attached, notes = mailer.pick_attachments([big, small, tmp_path / "missing.md"], limit=100)
    assert attached == [small]
    assert len(notes) == 1 and "big.wav" in notes[0]


class FakeSMTP:
    sent: ClassVar[list] = []

    def __init__(self, host, port, timeout):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context):
        self.tls = True

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, msg):
        FakeSMTP.sent.append((self.host, self.port, self.login_args, self.tls, msg["To"]))


def test_send_uses_tls_and_stored_password(monkeypatch, tmp_path):
    monkeypatch.setattr(
        mailer, "get_password", lambda user: "app-pass" if user == "me@example.org" else None
    )
    d, q = _digest(tmp_path)
    mailer.send(CFG, mailer.compose_daily(CFG, d, q), smtp_factory=FakeSMTP)
    assert FakeSMTP.sent[-1] == (
        "smtp.gmail.com",
        587,
        ("me@example.org", "app-pass"),
        True,
        "me@example.org",
    )


def test_send_without_password_explains_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(mailer, "get_password", lambda user: None)
    with pytest.raises(RuntimeError, match="radar email-setup"):
        mailer.send(CFG, mailer.build_message(CFG, "s", "b", []), smtp_factory=FakeSMTP)


def test_wav_to_mp3_shrinks_audio(tmp_path):
    wav = tmp_path / "e.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x10" * 22050 * 3)
    mp3 = wav_to_mp3(wav)
    assert mp3 is not None and mp3.suffix == ".mp3"
    assert 0 < mp3.stat().st_size < wav.stat().st_size / 4


def test_bitrate_choice_fits_budget():
    from radar.podcast import mp3_bitrate_for

    assert mp3_bitrate_for(23 * 60) == 80  # 23-minute NotebookLM episode, 16 MiB budget
    assert mp3_bitrate_for(60 * 60) == 32  # an hour still fits
    assert mp3_bitrate_for(23 * 60, budget_bytes=8 * 1024 * 1024) == 40


def test_oversize_m4a_is_reencoded_to_fit(tmp_path):
    import av
    import numpy as np

    from radar.podcast import duration_seconds, email_ready

    src = tmp_path / "episode.m4a"
    with av.open(str(src), "w") as out:
        st = out.add_stream("aac", rate=24000, layout="mono")
        t = np.arange(24000 * 20) / 24000
        samples = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        for i in range(0, len(samples), 1024):
            frame = av.AudioFrame.from_ndarray(
                samples[None, i : i + 1024], format="fltp", layout="mono"
            )
            frame.rate = 24000
            for pkt in st.encode(frame):
                out.mux(pkt)
        for pkt in st.encode(None):
            out.mux(pkt)
    small_budget = src.stat().st_size // 2
    mp3 = email_ready(src, budget_bytes=small_budget)
    assert mp3.suffix == ".mp3" and mp3.stat().st_size <= small_budget
    assert abs(duration_seconds(mp3) - 20) < 1.5
    assert email_ready(src, budget_bytes=10**9) == src  # fits: attached unchanged


def _body(tmp_path, build: dict | None):
    d, q = _digest(tmp_path)
    if build is not None:
        (d / "build.json").write_text(__import__("json").dumps(build), encoding="utf-8")
    msg = mailer.compose_daily(CFG, d, q, today=dt.date(2026, 11, 19))
    return msg.get_body(("plain",)).get_content()


def test_email_build_line_published(tmp_path):
    body = _body(
        tmp_path,
        {"status": "published", "package": "ase", "url": "https://github.com/o/r/x", "folder": "f"},
    )
    assert "**Daily build:** ase → https://github.com/o/r/x" in body
    assert "did not run" not in body


def test_email_build_line_did_not_run(tmp_path):
    body = _body(
        tmp_path,
        {"status": "did_not_run", "package": "ase", "url": "https://github.com/o/r/x"},
    )
    assert "**Daily build:** ase → https://github.com/o/r/x (did not run; see its README)" in body


def test_email_build_line_skipped_reason_only(tmp_path):
    body = _body(
        tmp_path,
        {"status": "skipped", "reason": "no suitable item", "folder": "secret-folder"},
    )
    assert "Daily build skipped: no suitable item" in body
    assert "secret-folder" not in body


def test_email_build_line_absent(tmp_path):
    assert "Daily build" not in _body(tmp_path, None)


def test_email_build_line_after_podcast_line(tmp_path):
    d, q = _digest(tmp_path)
    (d / "notebook_url.txt").write_text("https://notebooklm.google.com/x", encoding="utf-8")
    (d / "build.json").write_text(
        '{"status": "skipped", "reason": "build failed"}', encoding="utf-8"
    )
    body = mailer.compose_daily(CFG, d, q).get_body(("plain",)).get_content()
    assert body.index("**Podcast:**") < body.index("Daily build skipped") < body.index("#")


@pytest.mark.parametrize("status", ["failed", "blocked", "skipped"])
def test_email_build_line_uses_real_status(tmp_path, status):
    body = _body(tmp_path, {"status": status, "reason": "why"})
    assert f"Daily build {status}: why" in body
