# Spotisync

Application web auto-hébergée qui transforme une bibliothèque Spotify (titres likés,
playlists) en fichiers audio rangés et tagués, prêts pour Navidrome/Jellyfin/Plex.

- Liaison **Spotify OAuth PKCE** (client secret inutile), sélection des playlists à suivre
- Téléchargement via **spotDL** (audio YouTube Music, jamais le flux Spotify)
- **Métadonnées** nettoyées : artistes multiples (`A/B` → `A; B`), piste, année, ISRC, pochette
- **Anti-doublons** : ISRC puis titre/artiste proche + durée voisine, quarantaine plutôt que suppression
- **Bibliothèque** consultable, filtres par statut, index du dossier musique
- **Auto-sync** périodique des nouveautés
- **1 seul conteneur** (FastAPI + SQLite + HTMX), PUID/PGID, healthcheck

> ⚠️ L'audio ne provient pas de Spotify (DRM) mais de YouTube Music via spotDL : MP3/Opus sont
> des ré-encodages d'une source lossy. Le FLAC n'apporterait aucune qualité supplémentaire ;
> seul un achat/rip CD le permet.

## Prérequis Spotify

L'API Spotify exige que le **propriétaire de l'application developer ait un abonnement
Premium actif** (politique Spotify). Sans Premium, tous les appels renvoient
`403 Active premium subscription required for the owner of the app` — l'app l'affiche
explicitement au moment de la liaison.

1. https://developer.spotify.com/dashboard → **Create app**
2. Redirect URI : `http://<ip-unraid>:8080/callback`
3. Scopes utilisés : `user-library-read playlist-read-private playlist-read-collaborative`
4. Copier le **Client ID** dans les réglages de Spotisync, puis « Lier mon compte »

## Déploiement (Unraid / Docker)

```bash
docker compose up -d --build
```

Chemins par défaut :
- `/config` → `/mnt/user/appdata/spotisync` (base SQLite, staging, logs)
- `/music` → `/mnt/user/music` (dossier d'export, modifiable dans l'UI)
- port `8080`

Variables utiles : `PUID`, `PGID`, `TZ`, `SPOTISYNC_PORT`, `SPOTIFY_CLIENT_ID`,
`SPOTIFY_REDIRECT_URI`, `SPOTISYNC_MUSIC`.

### Template Unraid

Créer le conteneur manuellement avec : port `8080`, deux chemins (`/mnt/user/appdata/spotisync`
→ `/config`, `/mnt/user/music` → `/music`), variables `PUID=99`, `PGID=100`, `TZ=Europe/Paris`.
C'est tout — le schéma SQLite se crée au premier démarrage.

## Utilisation

1. **Réglages** : Client ID Spotify, redirect URI, dossier d'export, format/bitrate, parallélisme.
2. **Lier mon compte** → retour sur `/callback` → playlists importées.
3. **Playlists** : cocher celles à suivre → « Synchroniser la sélection ».
4. **Tableau de bord** : « Lancer le téléchargement » (limite optionnelle pour tester).
5. **Bibliothèque** → « Indexer le dossier musique » puis **Doublons** pour arbitrer.

## Développement

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
SPOTISYNC_CONFIG=./data SPOTISYNC_MUSIC=./music .venv/bin/uvicorn app.main:app --reload
.venv/bin/python -m pytest tests -q
```

Arborescence : `app/{config,db,spotify,sync,downloader,metadata,main}.py`,
`app/templates/*.html` (Jinja2 + Tailwind/HTMX via CDN).

## Limites connues

- Les titres locaux Spotify (`is_local`) et les fichiers sans ISRC échappent à la détection exacte.
- Le matching YouTube peut choisir une version live/remix ; l'écran d'arbitrage manuel n'est pas
  encore implémenté (v2).
- Un seul lot de téléchargement à la fois ; l'état est en base, donc reprise après redémarrage.
