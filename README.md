# Spotisync

Application web auto-hébergée qui transforme une bibliothèque Spotify (titres likés,
playlists) en fichiers audio rangés et tagués, prêts pour Navidrome/Jellyfin/Plex.

- Liaison **Spotify OAuth PKCE** (client secret inutile), sélection des playlists à suivre
- **Import de fichiers** (CSV Exportify, JSON d'export Spotify) : l'app reste utilisable sans Premium
- **Une seule page Playlists** pour gérer les playlists Spotify et les imports (badge de source,
  sélection, retrait d'un import), l'ancienne route `/import` redirige simplement vers elle
- Téléchargement via **spotDL** (audio YouTube Music, jamais le flux Spotify)
- **Métadonnées** nettoyées : artistes multiples (`A/B` → `A; B`), piste, année, ISRC, pochette
- **Anti-doublons** : ISRC puis titre/artiste proche + durée voisine, quarantaine plutôt que suppression
- **Bibliothèque** consultable, filtres par statut, index du dossier musique
- **Auto-sync** périodique des nouveautés
- **Reprise automatique** des téléchargements interrompus par un redémarrage du conteneur
  (désactivable dans les réglages)
- **Notifications Discord** par webhook : fin de téléchargement, échecs, synchronisation
  automatique, reprise après démarrage (bouton de test ; un webhook cassé ne bloque jamais un lot)
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

## Installation

### Unraid (template)

L'image n'est pas encore publiée sur un registre : construisez-la d'abord sur le serveur
(terminal Unraid, dans le dossier du dépôt) :

```bash
git clone https://github.com/guillian77/spotisync-.git /mnt/user/appdata/spotisync-src
cd /mnt/user/appdata/spotisync-src
docker build -t guillian77/spotisync:latest .
```

> **Mise à jour d'une installation existante** : `docker build` remplace l'image taguée, mais un
> conteneur déjà créé continue de tourner sur l'ancienne image — `docker restart` ne fait que
> relancer l'ancien code. Il faut **recréer** le conteneur :
> ```bash
> git pull && docker build -t guillian77/spotisync:latest .
> docker stop Spotisync && docker rm Spotisync    # puis Add Container → Template Spotisync → Apply
> ```
> Contrôle rapide de la version réellement exécutée :
> `docker exec Spotisync grep -c "_static_dir" /app/app/main.py` (0 = ancienne image).

Puis installez le template, au choix :

**a. Par l'interface (recommandé)**
1. Copier `unraid/spotisync.xml` dans `/boot/config/plugins/dockerMan/templates-user/`
   ```bash
   cp /mnt/user/appdata/spotisync-src/unraid/spotisync.xml /boot/config/plugins/dockerMan/templates-user/
   ```
2. Unraid → **Docker** → *Add Container* → sélectionner **Spotisync** dans le menu *Template*.
3. Vérifier les deux chemins (`/mnt/user/appdata/spotisync` → `/config`,
   `/mnt/user/music` → `/music`), ajuster `PUID`/`PGID` si votre partage musique ne
   appartient pas à `nobody:users`, puis **Apply**.

**b. En ligne de commande**

```bash
docker run -d --name spotisync --restart unless-stopped \
  -p 8080:8080 \
  -e PUID=99 -e PGID=100 -e TZ=Europe/Paris \
  -v /mnt/user/appdata/spotisync:/config \
  -v /mnt/user/music:/music \
  guillian77/spotisync:latest
```

Le schéma SQLite est créé au premier démarrage ; l'interface est sur
`http://<ip-unraid>:8080`. Le conteneur ne fait **jamais** de `chown` sur `/music`.

### Docker Compose (hors Unraid)

```bash
docker compose up -d --build
```

### Chemins, port et variables

- `/config` → `/mnt/user/appdata/spotisync` (SQLite, staging, journaux)
- `/music` → `/mnt/user/music` (dossier d'export, modifiable ensuite dans l'UI)
- port `8080` (interne et externe, `SPOTISYNC_PORT` pour changer côté conteneur)
- variables : `PUID`, `PGID`, `TZ`, `SPOTISYNC_PORT`, `SPOTISYNC_MUSIC`,
  `SPOTIFY_CLIENT_ID`, `SPOTIFY_REDIRECT_URI`, `DISCORD_WEBHOOK`
- santé : `GET /api/status` (touche la base, atteste aussi que le schéma existe)

## Utilisation

1. **Réglages** : Client ID Spotify, redirect URI, dossier d'export, format/bitrate, parallélisme.
2. **Lier mon compte** → retour sur `/callback` → playlists importées.
3. **Playlists** : cocher celles à suivre → « Synchroniser la sélection ». Le bloc d'import
   (CSV/JSON) est replié en haut de cette même page.
4. **Tableau de bord** : « Lancer le téléchargement » (limite optionnelle pour tester).
5. **Bibliothèque** → « Indexer le dossier musique » puis **Doublons** pour arbitrer.

### Sans Premium : import de fichiers

Le bloc **Importer une liste de titres** (page Playlists) accepte un **CSV Exportify** (https://exportify.app, fonctionne en compte
gratuit) ou un **JSON d'export Spotify** (*Compte → Confidentialité → Télécharger vos données*,
fichier `LikedSongs.json`). L'import crée une playlist « importée », déjà sélectionnée : le
téléchargement, les tags et le dédoublonnage se comportent exactement comme pour une playlist
Spotify. Réimporter un fichier mis à jour ne duplique pas les pistes (clé = identifiant Spotify,
sinon titre + artiste normalisés) ; une playlist importée ne se resynchronise pas depuis l'API,
il faut réimporter le fichier.

L'URL de redirection OAuth doit utiliser **HTTPS** (ou `http://127.0.0.1:PORT`, seule exception
autorisée par Spotify — une IP LAN comme `http://192.168.1.80:8080/callback` est refusée avec
`400 redirect_uri: Insecure`). Derrière un reverse proxy TLS, c'est transparent.

## Développement

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
SPOTISYNC_CONFIG=./data SPOTISYNC_MUSIC=./music .venv/bin/uvicorn app.main:app --reload
.venv/bin/python -m pytest tests -q
```

Arborescence : `app/{config,db,spotify,sync,downloader,metadata,main}.py`,
`app/templates/*.html` (Jinja2 + Tailwind/HTMX via CDN), `unraid/` (template + icône).

## Recherche unifiée

Champ de recherche sur le tableau de bord (et page `/search`), avec détection automatique :

| Saisie | Comportement |
|---|---|
| nom de titre / artiste / album | recherche locale dans la base (statut et motif d'erreur visibles) |
| nom de playlist | recherche locale parmi les playlists connues |
| ID (22 car.) ou URL de playlist Spotify | lecture de la page d'intégration publique — **sans API ni Premium** — puis import ; limité aux **50 premiers titres** |
| ID (11 car.) ou URL de vidéo YouTube | téléchargement de **cette vidéo exacte** via yt-dlp, avec option « remplacer un titre en échec » (les tags Spotify priment) |
| URL de playlist YouTube | liste les vidéos (jusqu'à 200), les ajoute comme pistes à télécharger, puis le lot se lance depuis le tableau de bord |

Un téléchargement YouTube autonome crée une entrée dans la bibliothèque (identifiant
`yt:<video>`), et **« Indexer le dossier musique »** adopte les fichiers présents sur le disque
qui n'ont pas encore de fiche : ils deviennent visibles dans la bibliothèque.

Le refus de retéléchargement d'une vidéo vérifie **la base et le disque** : un fichier présent
sans fiche (téléchargé par une version antérieure) est détecté par le chemin cible calculé, donc
plus de `(2).mp3` même si la fiche manque.

Anti-doublon à trois niveaux pour ces ajouts : même chemin déjà référencé, même ISRC, ou même
titre/artiste avec une durée à ±5 s (dans ce cas le fichier est rattaché au lieu de créer une
seconde fiche). Et retélécharger une vidéo déjà présente est refusé **avant** toute
consommation de bande passante.

Les titres des vidéos YouTube sont nettoyés des mentions promotionnelles (« (Video Oficial) »,
« [HD] », « (Clip officiel) »…) avant d'être enregistrés. La lecture d'une playlist Spotify
passe par `open.spotify.com/embed` : pas de pagination possible, donc pour une grande playlist
utilisez Exportify.

## Correspondance audio et garde-fous

spotDL peut échouer sur la résolution Spotify (bug de son client anonyme `SpotipyFree` :
`KeyError: 'uri'`, `LookupError: No results`) alors que YouTube Music fonctionne très bien.
L'app enchaîne donc les candidats dans cet ordre, et **recommence en recherche texte** dès que
la résolution Spotify échoue :

1. URL Spotify de la piste
2. `Artiste - Titre`
3. `Artiste - Titre Album`
4. `Artiste - Titre audio` et `Artiste - Titre Album audio`

Chaque candidat téléchargé est accepté ou refusé avant d'être rangé :

- **durée** : écart toléré = le maximum entre `duration_tolerance_s` (20 s par défaut) et
  10 % de la durée Spotify ; au-delà, le candidat est refusé. Un radio edit de 12 s plus court
  passe, une version live de 4 minutes non
- **versions live/concert** : si le résultat contient `live`, `concert`, `session`, `karaoke`…
  alors que le titre Spotify ne l'annonce pas → refusé (`reject_keywords`, correspondance sur
  mot entier : `alive` ne déclenche pas `live`)
- épuisement des candidats → statut `error` avec le motif exact des refus, rien n'est rangé

Les tags écrits viennent **de Spotify, pas de YouTube** (`trust_db_metadata`) : titre, artistes,
album, album_artist, numéro de piste, année, ISRC. YouTube ne fournit que l'audio et la pochette.

## Dépannage

- **Tous les téléchargements en erreur, message `PermissionError: [Errno 13] ... /root/.config`** :
  spotDL écrit son cache dans `$HOME/.config`. L'entrypoint force désormais
  `HOME=/config`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` et `TMPDIR` sous `/config` ;
  une image antérieure à la 0.2.1 doit être reconstruite.
- **`/music` non inscriptible** : le conteneur ne fait jamais de `chown` sur ce volume.
  Les réglages affichent « accessible en écriture » ou un avertissement rouge ; aligner
  `PUID`/`PGID` sur le propriétaire du partage (99:100 par défaut sur Unraid).
- **Les fichiers téléchargés appartiennent à `nobody`** : `PUID=99` est la valeur par défaut du
  template, elle ne convient qu'aux partages possédés par `nobody`. Pour un utilisateur nommé,
  mettre son UID (`id -u guillian` → 1000) et `PGID=100`, **recréer le conteneur**, puis
  `chown -R 1000:100 /mnt/user/<partage>`. Vérification : `docker exec Spotisync id app`.
- **Tous les titres en `KeyError: 'uri'` ou `LookupError: No results`** : le client Spotify
  anonyme de spotDL est cassé ou limité ; la 0.7.0 bascule automatiquement en recherche texte.
  Si l'erreur persiste, vérifiez `spotdl download "Artiste - Titre"` en direct dans le conteneur.
- **`HTTP Error 403: Forbidden` sur certaines vidéos YouTube** : blocage anti-robot. L'app
  réessaie automatiquement avec d'autres clients d'extraction (`android,web`, `ios`, `tv`).
  Si le blocage persiste, il faut fournir des cookies YouTube (non pris en charge à ce jour).
- **`Some YouTube downloads require Deno` / `YT-DLP download error`** : yt-dlp a besoin d'un
  runtime JS récent pour les challenges de YouTube ; le paquet Debian `nodejs` est trop ancien.
  L'image installe **Deno** (runtime par défaut) et le code retombe sur `node` s'il est absent.
  En cas de doute : `docker exec Spotisync deno --version`.
- **Après un redémarrage, des titres restent « en attente »** : la reprise automatique est
  désactivée dans les réglages, ou le compte n'a plus de piste `pending`. Les états
  `queued`/`running` laissés par un arrêt brutal sont remis en `pending` au démarrage.
- **`400 redirect_uri: Insecure`** : Spotify exige HTTPS, ou `http://127.0.0.1:PORT` pour
  une adresse de bouclage. `localhost` et les IP LAN sont refusés.
- **`403 Active premium subscription required for the owner of the app`** : politique
  Spotify, aucun contournement possible — utiliser l'onglet **Import** en attendant.

## Limites connues

- Les titres locaux Spotify (`is_local`) et les fichiers sans ISRC échappent à la détection exacte.
- Le matching YouTube peut choisir une version live/remix ; l'écran d'arbitrage manuel n'est pas
  encore implémenté (v2).
- Un seul lot de téléchargement à la fois ; l'état est en base, donc reprise après redémarrage.
