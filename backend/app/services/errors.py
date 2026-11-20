"""Erreur commune des services du manager.

Une seule exception pour tout ce qui échoue avec un message destiné à
l'utilisateur : reconstruction, essai, installation, sauvegardes, clients.
Les routes la traduisent en réponse HTTP.
"""

from __future__ import annotations


class ManagerError(RuntimeError):
    """Une opération n'a pas pu être menée à son terme."""
