"""Tables de cas partagées avec le lanceur (contract/cases/ : sync-decisions, names, limits).

Règle codée des deux côtés : les deux suites la vérifient. Ce fichier applique les tables à
`saves_sync.decide`, `saves.valid_game` / `clean_dir`, `machine_api` (identifiant de machine, empreinte,
taille maximale). La même table est lue par internal/savesync/contract_cases_test.go côté lanceur.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.services import machine_api, saves, saves_sync
from tests import contract_check as cc

CASES = cc.CONTRACT_PATH.parent / "cases"
LAUNCHER_CASES = Path(__file__).resolve().parents[3] / "wsquashfs-launcher" / "contract" / "cases"

# Vocabulaire du lanceur (decideSync) pour chaque décision du manager : la correspondance de l'en-tête
# de sync-decisions.yaml. « rien » n'a pas d'action côté lanceur : rien à transférer, donc « à jour ».
GO_OF = {"à jour": "à jour", "rien": "à jour", "remonter": "envoi", "descendre": "réception",
         "absente": "réception", "conflit": "conflit"}


def load(name: str):
    return yaml.safe_load((CASES / name).read_text(encoding="utf-8"))


def expand(item):
    """Un cas de table : une chaîne, ou {repeat, count} (texte répété, pour les bornes)."""
    if isinstance(item, dict):
        return item["repeat"] * item["count"]
    return item


SYNC = load("sync-decisions.yaml")["cases"]
NAMES = load("names.yaml")
LIMITS = load("limits.yaml")


# ------------------------------------------------------------------ la copie

def test_the_copied_tables_are_the_launchers_tables():
    if not LAUNCHER_CASES.exists():
        pytest.skip("dépôt du lanceur absent : tables non comparées (make contract les recopie)")
    ours = sorted(p.name for p in CASES.iterdir())
    assert ours == sorted(p.name for p in LAUNCHER_CASES.iterdir()), "tables manquantes ou en trop : make contract"
    for name in ours:
        assert (CASES / name).read_bytes() == (LAUNCHER_CASES / name).read_bytes(), f"{name} : make contract"


# ------------------------------------------------------------------ décisions de synchro

def test_the_sync_table_covers_every_manager_verdict():
    assert len(SYNC) >= 20
    assert {c["expect"]["manager"] for c in SYNC} == set(GO_OF)


@pytest.mark.parametrize("case", SYNC, ids=[c["name"] for c in SYNC])
def test_decide_matches_the_shared_table(case):
    client, manager = case["client"], case["manager"]
    action = saves_sync.decide(case["base"], client["fingerprint"], manager["fingerprint"],
                               client_newest=client["newest"], manager_newest=manager["newest"],
                               client_content=client.get("content", ""),
                               manager_content=manager.get("content", ""))
    assert action == case["expect"]["manager"]


@pytest.mark.parametrize("case", SYNC, ids=[c["name"] for c in SYNC])
def test_the_two_vocabularies_agree_in_the_table(case):
    """Le verdict du lanceur (go) est celui de la correspondance pour le verdict du manager."""
    assert case["expect"]["go"] == GO_OF[case["expect"]["manager"]], case["name"]


# ------------------------------------------------------------------ noms

GAMES = NAMES["games"]
MACHINES = NAMES["machines"]
DOSSIERS = NAMES["dossier_paths"]


@pytest.mark.parametrize("name", [expand(v) for v in GAMES["valid"]])
def test_valid_game_accepts_the_shared_valid_names(name):
    assert saves.valid_game(name), repr(name[:40])


@pytest.mark.parametrize("name", [expand(v) for v in GAMES["invalid"]])
def test_valid_game_refuses_the_shared_invalid_names(name):
    assert not saves.valid_game(name), repr(name[:40])


@pytest.mark.parametrize("name", [expand(v) for v in MACHINES["valid"]])
def test_machine_ids_accepted_match_the_shared_table(name):
    assert machine_api._MACHINE_HEADER.fullmatch(name), repr(name[:40])


@pytest.mark.parametrize("name", [expand(v) for v in MACHINES["invalid"]])
def test_machine_ids_refused_match_the_shared_table(name):
    assert not machine_api._MACHINE_HEADER.fullmatch(name), repr(name[:40])


@pytest.mark.parametrize("path", [expand(v) for v in DOSSIERS["valid"]])
def test_declared_dirs_accepted_match_the_shared_table(path):
    assert saves.clean_dir(path) == path, repr(path[:40])


@pytest.mark.parametrize("path", [expand(v) for v in DOSSIERS["invalid"]])
def test_declared_dirs_refused_match_the_shared_table(path):
    assert saves.clean_dir(path) is None, repr(path[:40])


# ------------------------------------------------------------------ limites et formats

def test_archive_limit_is_the_shared_limit():
    assert machine_api.MAX_ARCHIVE == LIMITS["archive_max_bytes"] == 2 * 1024 ** 3


def test_fingerprint_format_is_the_shared_format():
    assert machine_api._FINGERPRINT.pattern == LIMITS["fingerprint"]["pattern"]
    for fp in LIMITS["fingerprint"]["valid"]:
        assert machine_api._FINGERPRINT.fullmatch(fp), fp
    for fp in LIMITS["fingerprint"]["invalid"]:
        assert not machine_api._FINGERPRINT.fullmatch(fp), repr(fp)
