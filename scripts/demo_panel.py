"""Serveur de démonstration : simule un lot en cours pour vérifier le panneau de téléchargement."""
import sys
import threading
import time
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # « app » est à la racine du dépôt


def fake_batch() -> None:
    time.sleep(2.0)
    from app.downloader import manager

    manager._pool = object()          # « lot actif »
    manager.total = 7
    manager.done = 2
    manager.failed = 1
    manager.current = ["Jul — Ce soir"]
    manager.stopping = False
    time.sleep(float(__import__('os').environ.get('DEMO_HOLD', '25')))  # laisse le temps de regarder
    manager._pool = None
    manager.current = []
    for _ in range(8):                # plusieurs occasions de capter le toast
        manager.notice = {"kind": "info", "message": "Lot terminé — 2 réussi(s), 1 échec(s)"}
        time.sleep(8)


threading.Thread(target=fake_batch, daemon=True).start()
uvicorn.run("app.main:app", host="127.0.0.1", port=8074)
