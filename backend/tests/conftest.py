"""Isolation des tests : jamais la configuration ni la ludothèque réelles.

Les valeurs par défaut des réglages (config.json, dossier d'état, sauvegardes)
dépendent du dossier personnel et sont calculées à l'import de `app.config` :
HOME est donc remplacé ici, avant tout import de l'application, par un dossier
temporaire propre à la session de tests. Les variables WSQUASHFS_MGR_* de
l'environnement sont retirées pour la même raison.
"""

from __future__ import annotations

import os
import tempfile

_HOME = tempfile.mkdtemp(prefix="wsqfs-tests-home-")
os.environ["HOME"] = _HOME
for _name in [n for n in os.environ if n.startswith("WSQUASHFS_MGR_")]:
    del os.environ[_name]
