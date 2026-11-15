"""Notifications Discord (webhook) : envoi best-effort, jamais bloquant."""
import httpx

from . import db


def webhook() -> str:
    return (db.setting("discord_webhook", "") or "").strip()


def send(content: str, title: str = "") -> tuple[bool, str]:
    """Envoie un message. Renvoie (succès, détail) — les erreurs ne remontent jamais
    jusqu'à l'appelant : une notification ratée ne doit pas casser un téléchargement."""
    url = webhook()
    if not url:
        return False, "aucun webhook configuré"
    if not url.startswith(("https://discord.com/api/webhooks/",
                           "https://discordapp.com/api/webhooks/",
                           "https://canary.discord.com/api/webhooks/")):
        return False, "URL de webhook Discord invalide"
    payload: dict = {"content": content[:1900]}
    if title:
        payload["embeds"] = [{"title": title[:250], "description": content[:1900],
                              "color": 0x34D399}]
        payload.pop("content")
    try:
        r = httpx.post(url, json=payload, timeout=10)
    except Exception as exc:  # noqa: BLE001
        return False, f"envoi impossible : {exc}"[:200]
    if r.status_code in (200, 204):
        return True, "envoyé"
    return False, f"HTTP {r.status_code} : {r.text[:160]}"


def _enabled(key: str) -> bool:
    return db.setting(key, "0") == "1"


def download_finished(done: int, failed: int, total: int) -> None:
    if not webhook():
        return
    if failed and _enabled("notify_download_errors"):
        send(f"⚠️ **Téléchargement terminé avec des échecs**\n"
             f"{done}/{total} réussis · **{failed} échec(s)**\n"
             f"Vous pouvez les relancer depuis le tableau de bord.")
    elif not failed and _enabled("notify_download_done"):
        send(f"✅ **Téléchargement terminé**\n{done}/{total} titres récupérés.")


def autosync_finished(new_tracks: int, playlists: int) -> None:
    if not _enabled("notify_autosync") or not webhook() or new_tracks <= 0:
        return
    send(f"🔔 **Synchronisation automatique**\n"
         f"{new_tracks} nouveau(x) titre(s) détecté(s) sur {playlists} playlist(s).")


def test() -> tuple[bool, str]:
    return send("🧪 Test de notification Spotisync — le webhook fonctionne.",
                title="Spotisync")
