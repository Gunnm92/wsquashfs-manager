"""Sauvegardes entre clients : état par machine, jaquette Switch, pull, push et
suppression (tâches « saves » de la file, suivies par /api/tasks/{id})."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from .. import deps, library_index
from ..deps import QueueDep, SettingsDep
from ..services import clients as clients_svc
from ..services import saves as saves_svc
from ..services import saves_status as status_service
from ..services import saves_store
from ..services.artwork import switch_artwork

router = APIRouter()


@router.get("/api/saves/art")
def saves_art(settings: SettingsDep, game: str):
    """Jaquette d'un jeu Switch de l'onglet Sauvegardes (« switch:<TitleID> »), d'après les
    gamelist.xml de switch/ et switch2/ des dossiers roms/. Les jeux PC prennent celle de leur image
    (/api/image/art) ; un dossier déclaré n'en a pas : 404."""
    if not game.startswith(saves_svc.SWITCH_PREFIX):
        raise HTTPException(status_code=404, detail="aucune illustration")
    art = switch_artwork(settings.roms_roots or settings.roms_dirs, game[len(saves_svc.SWITCH_PREFIX):])
    if art is None:
        raise HTTPException(status_code=404, detail="aucune illustration")
    return FileResponse(art.path, media_type=art.media_type,
                        headers={"X-Art-Kind": art.kind, "Cache-Control": "private, max-age=3600"})


@router.get("/api/saves/status")
def saves_status(settings: SettingsDep, id: str | None = None, refresh: bool = False):
    """État par machine (identique, plus avancée, en retard, absente, injoignable) avec sa
    taille, son système et son icône ; par jeu : le manager, la suggestion (check,
    check_by), l'action décidée par client (`action`) et les versions de conflit à départager
    (`conflicts`). Sans id : tous les jeux connus ; avec id d'image : ce jeu. Un client injoignable
    est sauté pendant 30 s ; refresh=1 l'interroge quand même."""
    games = [saves_svc.game_name(library_index.find_image(settings, id).path.name)] if id is not None else None
    return status_service.status(settings, games, refresh=refresh)


class SavesSelection(BaseModel):
    # Noms de jeux (comme le lanceur : nom de l'image sans .wsquashfs) ; un jeu installé
    # seulement sur un client n'a pas forcément d'image dans la bibliothèque du manager
    games: list[str] | None = None
    all: bool = False                   # tous les jeux : de la source, ou du manager

    @model_validator(mode="after")
    def _one_of(self) -> SavesSelection:
        if self.all == bool(self.games):
            raise ValueError("games (liste non vide) ou all (vrai) : l'un des deux")
        return self


class SavesPull(SavesSelection):
    source: str = Field(min_length=1, max_length=64)


class SavesPush(SavesSelection):
    # absent : depuis le manager ; « auto » : pour chaque jeu, depuis le client le plus avancé
    source: str | None = Field(default=None, max_length=64)
    targets: list[str] = Field(min_length=1, max_length=20)


def _saves_games(selection: SavesSelection) -> list[str] | None:
    """Jeux désignés (None : tous). 422 si un nom est invalide."""
    if selection.all:
        return None
    for game in selection.games or []:
        if not saves_svc.valid_game(game):
            raise HTTPException(status_code=422, detail=f"nom de jeu invalide : {game}")
    return list(dict.fromkeys(selection.games or []))


@router.post("/api/saves/pull")
def saves_pull(settings: SettingsDep, queue: QueueDep, body: SavesPull):
    """Sauvegardes d'un client source vers le manager (tâche « saves »). Source « auto » :
    chaque jeu depuis sa machine la plus avancée."""
    games = _saves_games(body)
    if body.source == "auto":
        params = {"op": "pull_auto", "games": games}
        title = "sauvegardes : manager ← plus avancée"
    else:
        source = deps.saves_machine(settings, body.source)
        params = {"op": "pull", "source": body.source, "games": games}
        title = f"sauvegardes : récupérer depuis {source['name']}"
    task = queue.submit("saves", None, None, params, title)
    return {"task": task.id}


@router.post("/api/saves/push")
def saves_push(settings: SettingsDep, queue: QueueDep, body: SavesPush):
    """Manager (sans source) ou machine source (push_from) vers les cibles (tâche « saves »)."""
    if len(set(body.targets)) != len(body.targets):
        raise HTTPException(status_code=422, detail="client cible en double")
    targets = [deps.saves_machine(settings, machine_id) for machine_id in body.targets]
    games = _saves_games(body)
    if body.source == "auto":            # chaque jeu depuis sa machine la plus avancée
        params = {"op": "push_auto", "targets": body.targets, "games": games}
        title = f"sauvegardes : plus avancée → {len(targets)} client(s)"
    elif body.source is not None:
        source = deps.saves_machine(settings, body.source)
        if body.source in body.targets:
            raise HTTPException(status_code=422, detail="la source ne peut pas être une cible")
        params = {"op": "push_from", "source": body.source, "targets": body.targets, "games": games}
        title = f"sauvegardes : {source['name']} → {len(targets)} client(s)"
    else:
        reserve = set(saves_store.reserve_games(settings))
        if games is None and not reserve:
            raise HTTPException(status_code=422, detail="manager vide : rien à envoyer")
        missing = [game for game in games or [] if game not in reserve]
        if missing:
            raise HTTPException(status_code=422, detail=f"absent du manager : {', '.join(missing)}")
        params = {"op": "push", "targets": body.targets, "games": games}
        title = f"sauvegardes : manager → {len(targets)} client(s)"
    task = queue.submit("saves", None, None, params, title)
    return {"task": task.id}


class SavesDelete(BaseModel):
    games: list[str] = Field(min_length=1, max_length=200)     # noms de jeux, comme ceux de l'onglet
    machines: list[str] = Field(default_factory=list, max_length=20)
    manager: bool = False                                       # aussi sur le manager


@router.post("/api/saves/delete")
def saves_delete(settings: SettingsDep, queue: QueueDep, body: SavesDelete):
    """Supprime les sauvegardes de jeux sur des clients (archivées puis effacées) et/ou sur
    le manager (version courante datée) : tâche « saves »."""
    for game in body.games:
        if not saves_svc.valid_game(game):
            raise HTTPException(status_code=422, detail=f"nom de jeu invalide : {game}")
    if len(set(body.games)) != len(body.games):
        raise HTTPException(status_code=422, detail="jeu en double")
    if len(set(body.machines)) != len(body.machines):
        raise HTTPException(status_code=422, detail="client en double")
    if not body.machines and not body.manager:
        raise HTTPException(status_code=422, detail="aucun client ni le manager : rien à supprimer")
    targets = [deps.saves_machine(settings, machine_id) for machine_id in body.machines]
    where = " et ".join(part for part in (f"{len(targets)} client(s)" if targets else "",
                                          "le manager" if body.manager else "") if part)
    params = {"op": "delete", "machines": body.machines, "games": body.games, "manager": body.manager}
    title = f"sauvegardes : suppression de {len(body.games)} jeu(x) sur {where}"
    task = queue.submit("saves", None, None, params, title)
    return {"task": task.id}


class SavesResolve(BaseModel):
    game: str = Field(min_length=1, max_length=200)
    keep: str = Field(min_length=1, max_length=300)     # « manager », le nom d'une version de conflit ou « client:<id> »
    push: bool = True                                   # ensuite, envoyer aux clients qui ne sont pas à jour


@router.post("/api/saves/resolve")
def saves_resolve(settings: SettingsDep, queue: QueueDep, body: SavesResolve):
    """Départage les versions de conflit d'un jeu : garde la version du manager, en retient une ou prend la copie d'un
    client (« client:<id> ») (tâche « saves »). 422 si le jeu, la version ou le client est inconnu."""
    if not saves_svc.valid_game(body.game):
        raise HTTPException(status_code=422, detail=f"nom de jeu invalide : {body.game}")
    if body.keep.startswith("client:"):
        try:
            clients_svc.find_machine(settings, body.keep[len("client:"):])
        except KeyError:
            raise HTTPException(status_code=422, detail=f"client inconnu : {body.keep[len('client:'):]}") from None
    elif body.keep != "manager" and body.keep not in {v["name"] for v in saves_store.conflict_versions(settings, body.game)}:
        raise HTTPException(status_code=422, detail=f"version de conflit inconnue : {body.keep}")
    params = {"op": "resolve", "game": body.game, "keep": body.keep, "push": body.push}
    title = f"sauvegardes : départager {body.game} ({'manager' if body.keep == 'manager' else 'version retenue'})"
    task = queue.submit("saves", None, None, params, title)
    return {"task": task.id}


@router.get("/api/saves/versions")
def saves_versions(settings: SettingsDep, game: str):
    """Versions précédentes d'un jeu gardées par le manager, la plus récente d'abord."""
    if not saves_svc.valid_game(game):
        raise HTTPException(status_code=422, detail=f"nom de jeu invalide : {game}")
    return {"versions": saves_store.archive_versions(settings, game)}


class SavesRestore(BaseModel):
    game: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=40)   # horodatage d'une version précédente
    push: bool = True                                   # ensuite, envoyer aux clients en retard


@router.post("/api/saves/restore")
def saves_restore(settings: SettingsDep, queue: QueueDep, body: SavesRestore):
    """Une version précédente redevient la copie du manager (la courante est archivée) ; ensuite,
    envoi aux clients en retard si `push` (tâche « saves »). 422 si le jeu ou la version est inconnu."""
    if not saves_svc.valid_game(body.game):
        raise HTTPException(status_code=422, detail=f"nom de jeu invalide : {body.game}")
    if body.version not in saves_store.archives(settings, body.game):
        raise HTTPException(status_code=422, detail=f"version inconnue : {body.version}")
    params = {"op": "restore", "game": body.game, "version": body.version, "push": body.push}
    task = queue.submit("saves", None, None, params, f"sauvegardes : restaurer {body.game}")
    return {"task": task.id}
