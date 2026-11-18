#!/bin/bash
# Mise à jour de Spotisync à la manière d'Unraid :
#   git pull  ->  docker build  ->  recréation du conteneur depuis SON TEMPLATE Unraid
# (équivalent du bouton « Apply Update → RECREATE » de l'interface Docker).
#
# Rien n'est codé en dur : image, ports, chemins, variables et politique de redémarrage
# sont lus dans /boot/config/plugins/dockerMan/templates-user/<Nom>.xml, c'est-à-dire ce
# que l'interface Docker d'Unraid a enregistré pour ce conteneur.
set -euo pipefail

SRC_DIR="/mnt/user/appdata/spotisync-src"
NAME="Spotisync"
TEMPLATE="/boot/config/plugins/dockerMan/templates-user/${NAME}.xml"
BUILD_IMAGE="guillian77/spotisync:latest"

cd "$SRC_DIR"

echo "→ git pull"
git pull

echo "→ docker build"
docker build -t "$BUILD_IMAGE" .

if [ ! -f "$TEMPLATE" ]; then
  echo "!! template introuvable : $TEMPLATE"
  echo "   le conteneur doit avoir été créé depuis l'interface Docker d'Unraid"
  exit 1
fi

# --- lecture du template : ses <Config> sont la source de vérité ------------
XML=$(tr '\n' ' ' < "$TEMPLATE")
IMAGE=$(sed -n 's:.*<Repository>\([^<]*\)</Repository>.*:\1:p' <<<"$XML")
IMAGE=${IMAGE:-$BUILD_IMAGE}
RESTART=$(sed -n 's:.*<ExtraParams>\([^<]*\)</ExtraParams>.*:\1:p' <<<"$XML")
RESTART=${RESTART:---restart=unless-stopped}
NETWORK=$(sed -n 's:.*<Network>\([^<]*\)</Network>.*:\1:p' <<<"$XML")
NETWORK=${NETWORK:-bridge}

PORTS=(); VARS=(); VOLS=()
while IFS= read -r cfg; do
  type=$(sed -n 's:.*Type="\([^"]*\)".*:\1:p' <<<"$cfg")
  target=$(sed -n 's:.*Target="\([^"]*\)".*:\1:p' <<<"$cfg")
  value=$(sed -n 's:.*>\([^<]*\)</Config>.*:\1:p' <<<"$cfg")
  [ -z "$type" ] && continue
  case "$type" in
    Port)     [ -n "$value" ] && PORTS+=(-p "${value}:${target}") ;;
    Path)     [ -n "$value" ] && VOLS+=(-v "${value}:${target}") ;;
    Variable) [ -n "$value" ] && VARS+=(-e "${target}=${value}") ;;
  esac
done < <(grep -o '<Config [^>]*>[^<]*</Config>' <<<"$XML")

echo "→ image      : $IMAGE"
echo "→ réseau     : $NETWORK ($RESTART)"
echo "→ ports      : ${PORTS[*]:-aucun}"
echo "→ volumes    : ${VOLS[*]:-aucun}"
echo "→ variables  : ${VARS[*]:-aucune}"

# --- recréation -------------------------------------------------------------
echo "→ arrêt et suppression du conteneur (les volumes restent intacts)"
docker stop "$NAME" >/dev/null 2>&1 || true
docker rm "$NAME" >/dev/null 2>&1 || true

echo "→ démarrage sur la nouvelle image"
docker run -d --name "$NAME" "--restart=${RESTART#--restart=}" --network "$NETWORK" \
  "${PORTS[@]}" "${VARS[@]}" "${VOLS[@]}" "$IMAGE" >/dev/null

# --- vérification -----------------------------------------------------------
sleep 8
if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = "true" ]; then
  echo "✓ $NAME démarré"
  docker logs --tail 8 "$NAME" 2>&1 | sed 's/^/  /'
else
  echo "✗ échec du démarrage, logs :"
  docker logs --tail 30 "$NAME" 2>&1 | sed 's/^/  /'
  exit 1
fi
