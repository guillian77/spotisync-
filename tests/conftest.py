"""Configuration de test : isole la base et le dossier musique avant tout import de l'app.

Sans ça, `app.config` fige CONFIG_DIR=/config au premier import (fait par test_core),
et les tests qui écrivent en base échouent avec PermissionError.
"""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="spotisync-tests-"))
os.environ["SPOTISYNC_CONFIG"] = str(_TMP / "config")
os.environ["SPOTISYNC_MUSIC"] = str(_TMP / "music")
os.environ.setdefault("SPOTDL_BIN", "/bin/false")
