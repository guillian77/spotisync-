"""Tests des notifications Discord (sans réseau réel)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import notify  # noqa: E402


def _patch_setting(monkeypatch, value: str) -> None:
    monkeypatch.setattr(notify.db, "setting", lambda key, default="": value)


def test_no_webhook_configured(monkeypatch):
    _patch_setting(monkeypatch, "")
    ok, msg = notify.send("test")
    assert ok is False
    assert "aucun webhook" in msg


def test_rejects_non_discord_url(monkeypatch):
    _patch_setting(monkeypatch, "https://example.com/hook")
    ok, msg = notify.send("test")
    assert ok is False
    assert "invalide" in msg


def test_send_success(monkeypatch):
    _patch_setting(monkeypatch, "https://discord.com/api/webhooks/123/abc")
    calls = {}

    class Resp:
        status_code = 204
        text = ""

    def fake_post(url, json=None, timeout=None):
        calls.update(url=url, payload=json)
        return Resp()

    monkeypatch.setattr(notify.httpx, "post", fake_post)
    ok, msg = notify.send("bonjour")
    assert ok is True and msg == "envoyé"
    assert calls["payload"]["content"] == "bonjour"
    assert calls["url"].startswith("https://discord.com/api/webhooks/")


def test_send_http_error(monkeypatch):
    _patch_setting(monkeypatch, "https://discord.com/api/webhooks/123/abc")

    class Resp:
        status_code = 404
        text = "Unknown Webhook"

    monkeypatch.setattr(notify.httpx, "post", lambda url, json=None, timeout=None: Resp())
    ok, msg = notify.send("bonjour")
    assert ok is False
    assert "HTTP 404" in msg


def test_download_finished_only_when_enabled(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda content, title="": sent.append(content) or (True, "ok"))
    monkeypatch.setattr(notify, "webhook", lambda: "https://discord.com/api/webhooks/1/x")
    monkeypatch.setattr(notify, "_enabled", lambda key: key == "notify_download_done")

    notify.download_finished(done=10, failed=0, total=10)
    assert len(sent) == 1 and "terminé" in sent[0]

    sent.clear()
    notify.download_finished(done=8, failed=2, total=10)  # échecs : case dédiée désactivée
    assert sent == []


def test_autosync_ignores_empty_batch(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda content, title="": sent.append(content) or (True, "ok"))
    monkeypatch.setattr(notify, "webhook", lambda: "https://discord.com/api/webhooks/1/x")
    monkeypatch.setattr(notify, "_enabled", lambda key: True)

    notify.autosync_finished(new_tracks=0, playlists=2)
    assert sent == []
    notify.autosync_finished(new_tracks=3, playlists=2)
    assert len(sent) == 1 and "3 nouveau" in sent[0]


def test_resume_notification_respects_toggle(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda content, title="": sent.append(content) or (True, "ok"))
    monkeypatch.setattr(notify, "webhook", lambda: "https://discord.com/api/webhooks/1/x")

    monkeypatch.setattr(notify, "_enabled", lambda key: key == "notify_resume")
    notify.resume_started(pending=4)
    assert len(sent) == 1 and "Reprise automatique" in sent[0] and "4" in sent[0]

    sent.clear()
    monkeypatch.setattr(notify, "_enabled", lambda key: False)
    notify.resume_started(pending=4)
    assert sent == []

    sent.clear()
    monkeypatch.setattr(notify, "_enabled", lambda key: True)
    monkeypatch.setattr(notify, "webhook", lambda: "")
    notify.resume_started(pending=4)  # pas de webhook : rien, et pas d'erreur
    assert sent == []
