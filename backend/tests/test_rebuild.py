"""Reconstruction transactionnelle : vraies images squashfs (mode extraction,
le seul disponible sans /dev/fuse) et logique pure (arborescence attendue)."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from app.config import Settings
from app.services import rebuild
from app.services.rebuild import (
    Changes,
    Job,
    RebuildCancelled,
    RebuildError,
    expected_listing,
    purge_backups,
    restore_backup,
    safe_rel,
    validate_backup,
)

needs_tools = pytest.mark.skipif(
    not (shutil.which("mksquashfs") and shutil.which("unsquashfs")),
    reason="squashfs-tools absent",
)


def _cat(image: Path, member: str) -> bytes:
    return subprocess.run(["unsquashfs", "-cat", str(image), member],
                          capture_output=True, check=True).stdout


def _listing(image: Path) -> set[str]:
    out = subprocess.run(["unsquashfs", "-l", str(image)], capture_output=True,
                         check=True).stdout.decode()
    return {ln[len("squashfs-root/"):] for ln in out.splitlines()
            if ln.startswith("squashfs-root/")}


@pytest.fixture
def image(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "Game Dir").mkdir(parents=True)
    (src / "Game Dir" / "game.exe").write_bytes(b"MZ" + b"\0" * 5000)
    (src / "Game Dir" / "old.dll").write_bytes(b"dll")
    (src / "autorun.cmd").write_bytes(b"REM test\r\nCMD=game.exe\r\nDIR=Game Dir\r\n")
    out = tmp_path / "roms" / "win" / "Jeu.wsquashfs"
    out.parent.mkdir(parents=True)
    subprocess.run(["mksquashfs", str(src), str(out), "-comp", "zstd", "-noappend", "-quiet"],
                   check=True, capture_output=True)
    return out


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(tmp_dir=tmp_path / "tmp", rebuild_mode="extract")


# ------------------------------------------------------------------ logique pure

def test_safe_rel_refuses_escapes():
    assert safe_rel("\\drive_c\\game\\a.dll") == "drive_c/game/a.dll"
    assert safe_rel("a/./b") == "a/b"
    for bad in ("", "/", "../etc/passwd", "a/../../b", "a/.."):
        with pytest.raises(RebuildError):
            safe_rel(bad)


def test_expected_listing_adds_parents_and_removes_subtrees():
    old = ["a", "a/x.txt", "a/sub", "a/sub/y", "ab", "autorun.cmd"]
    changes = Changes(write={"new/dir/f.txt": b"", "autorun.cmd": b"x"}, delete=["a/sub"])
    assert expected_listing(old, changes) == {
        "a", "a/x.txt", "ab", "autorun.cmd", "new", "new/dir", "new/dir/f.txt",
    }


# ------------------------------------------------------------------ vraies images

@needs_tools
def test_rebuild_replaces_autorun_and_keeps_old(image: Path, settings: Settings):
    before = image.read_bytes()
    listing_before = _listing(image)
    phases = []
    job = Job(on_progress=lambda phase, f: phases.append((phase, f)))
    new = b"REM test\r\nCMD=game.exe\r\nDIR=Game Dir\r\nGAME_VERSION=1.2\r\n"

    result = rebuild.rebuild(settings, image, Changes(write={"autorun.cmd": new}), job)

    assert result.mode == "extract"
    assert _cat(image, "autorun.cmd") == new
    assert _listing(image) == listing_before
    assert image.with_name("Jeu.wsquashfs.old").read_bytes() == before
    assert not image.with_name("Jeu.wsquashfs.part").exists()
    assert phases[-1] == ("terminé", 1.0)
    assert any(phase == "mksquashfs" for phase, _ in phases)


@needs_tools
def test_rebuild_adds_and_deletes_files(image: Path, settings: Settings):
    patch = image.parent / "patch.dll"
    patch.write_bytes(b"P" * 1000)
    changes = Changes(copy={"Game Dir/plugins/new.dll": patch}, delete=["Game Dir/old.dll"])
    rebuild.rebuild(settings, image, changes)

    files = _listing(image)
    assert "Game Dir/plugins/new.dll" in files
    assert "Game Dir/old.dll" not in files
    assert _cat(image, "Game Dir/plugins/new.dll") == b"P" * 1000


@needs_tools
def test_rebuild_refuses_when_old_exists(image: Path, settings: Settings):
    image.with_name(image.name + ".old").write_bytes(b"backup")
    with pytest.raises(RebuildError, match="sauvegarde existante"):
        rebuild.rebuild(settings, image, Changes(write={"autorun.cmd": b"CMD=x\r\n"}))


@needs_tools
def test_game_started_during_rebuild_keeps_original(image: Path, settings: Settings):
    before = image.read_bytes()
    calls = []

    def in_use(_image: Path) -> bool:
        calls.append(1)
        return len(calls) > 1        # libre au départ, lancé avant l'échange

    with pytest.raises(RebuildError, match="lancé pendant"):
        rebuild.rebuild(settings, image, Changes(write={"autorun.cmd": b"CMD=x\r\n"}),
                        in_use=in_use)
    assert image.read_bytes() == before
    assert not image.with_name(image.name + ".old").exists()
    assert not image.with_name(image.name + ".part").exists()


@needs_tools
def test_failed_verification_leaves_original(image: Path, settings: Settings, monkeypatch):
    before = image.read_bytes()

    def bad_apply(root: Path, changes: Changes) -> None:
        (root / "autorun.cmd").write_bytes(b"autre chose")

    monkeypatch.setattr(rebuild, "apply_changes", bad_apply)
    with pytest.raises(RebuildError, match="vérification"):
        rebuild.rebuild(settings, image, Changes(write={"autorun.cmd": b"CMD=x\r\n"}))
    assert image.read_bytes() == before
    assert not image.with_name(image.name + ".part").exists()


@needs_tools
def test_cancel_cleans_up(image: Path, settings: Settings, monkeypatch):
    before = image.read_bytes()
    job = Job()
    real = rebuild.run_progress

    def cancel_then_run(command, job_, phase, start, end):
        job_.cancel()
        real(command, job_, phase, start, end)

    monkeypatch.setattr(rebuild, "run_progress", cancel_then_run)
    with pytest.raises(RebuildCancelled):
        rebuild.rebuild(settings, image, Changes(write={"autorun.cmd": b"CMD=x\r\n"}), job)
    assert image.read_bytes() == before
    assert not image.with_name(image.name + ".part").exists()
    assert settings.tmp_dir is not None
    assert list(settings.tmp_dir.iterdir()) == []


@needs_tools
def test_space_check_blocks_before_work(image: Path, settings: Settings, monkeypatch):
    monkeypatch.setattr(rebuild.shutil, "disk_usage",
                        lambda path: shutil._ntuple_diskusage(10**12, 10**12, 10))
    with pytest.raises(RebuildError, match="espace insuffisant"):
        rebuild.rebuild(settings, image, Changes(write={"autorun.cmd": b"CMD=x\r\n"}))


def test_overlay_mode_requires_fuse(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(rebuild, "fuse_available", lambda settings: False)
    with pytest.raises(RebuildError, match="overlay"):
        rebuild.choose_mode(Settings(rebuild_mode="overlay"))
    assert rebuild.choose_mode(Settings(rebuild_mode="auto")) == "extract"


# ------------------------------------------------------------------ .old

def test_validate_and_restore_backup(tmp_path: Path):
    image = tmp_path / "Jeu.wsquashfs"
    image.write_bytes(b"new")
    image.with_name("Jeu.wsquashfs.old").write_bytes(b"old")
    restore_backup(image)
    assert image.read_bytes() == b"old"
    assert not image.with_name("Jeu.wsquashfs.old").exists()
    with pytest.raises(RebuildError):
        validate_backup(image)

    image.with_name("Jeu.wsquashfs.old").write_bytes(b"old2")
    validate_backup(image)
    assert image.read_bytes() == b"old"
    assert not image.with_name("Jeu.wsquashfs.old").exists()


def test_purge_backups_respects_retention(tmp_path: Path):
    image = tmp_path / "Jeu.wsquashfs"
    image.with_name("Jeu.wsquashfs.old").write_bytes(b"old")
    assert purge_backups([image], None) == []
    assert purge_backups([image], 1) == []          # tout juste échangé
    assert purge_backups([image], -1) == [image.with_name("Jeu.wsquashfs.old")]


# ------------------------------------------------------------------ progression

def test_run_progress_parses_percentages(tmp_path: Path):
    script = tmp_path / "p.sh"
    script.write_text("#!/bin/sh\necho 'Parallel mksquashfs'\necho 10\necho 50\necho 100\n")
    script.chmod(0o755)
    seen = []
    rebuild.run_progress([str(script)], Job(on_progress=lambda p, f: seen.append(f)),
                          "mksquashfs", 0.5, 1.0)
    assert seen == [0.55, 0.75, 1.0]


def test_run_progress_reports_tail_on_failure(tmp_path: Path):
    script = tmp_path / "p.sh"
    script.write_text("#!/bin/sh\necho 'FATAL ERROR: no space'\nexit 1\n")
    script.chmod(0o755)
    with pytest.raises(RebuildError, match="no space"):
        rebuild.run_progress([str(script)], Job(), "mksquashfs", 0, 1)


def test_run_progress_cancel_kills_process(tmp_path: Path):
    script = tmp_path / "p.sh"
    script.write_text("#!/bin/sh\nexec sleep 30\n")
    script.chmod(0o755)
    job = Job()
    threading.Timer(0.2, job.cancel).start()
    with pytest.raises(RebuildCancelled):
        rebuild.run_progress([str(script)], job, "mksquashfs", 0, 1)


@needs_tools
def test_default_work_dir_is_next_to_image_and_removed(image: Path, tmp_path: Path, monkeypatch):
    """Sans tmp_dir, le travail se fait à côté de l'image (même disque) ;
    le dossier caché disparaît ensuite."""
    seen = []
    real = rebuild._build_extract

    def spy(settings, image_, part, work, changes, job, owner_opts, existing=None):
        seen.append(work)
        real(settings, image_, part, work, changes, job, owner_opts, existing)

    monkeypatch.setattr(rebuild, "_build_extract", spy)
    settings = Settings(rebuild_mode="extract")
    rebuild.rebuild(settings, image, Changes(write={"autorun.cmd": b"CMD=x\r\n"}))
    assert seen[0].parent == image.parent / ".wsquashfs-manager"
    assert not (image.parent / ".wsquashfs-manager").exists()


def test_fallback_reason_is_explained(monkeypatch, tmp_path):
    monkeypatch.setattr(rebuild.Path, "exists", lambda self: str(self) != "/dev/fuse")
    assert "/dev/fuse absent" in rebuild.fuse_missing(Settings())


@needs_tools
def test_rebuilt_image_belongs_to_service_user(tmp_path: Path):
    """Image root (Batocera) : la nouvelle image appartient à l'utilisateur du
    service (arcade dans SteamBox), ce que le lanceur sans privilèges préfère."""
    src = tmp_path / "src"
    (src / "Game").mkdir(parents=True)
    (src / "Game" / "game.exe").write_bytes(b"MZ")
    (src / "autorun.cmd").write_bytes(b"CMD=game.exe\r\nDIR=Game\r\n")
    image = tmp_path / "Root.wsquashfs"
    subprocess.run(["mksquashfs", str(src), str(image), "-noappend", "-quiet",
                    "-force-uid", "0", "-force-gid", "0"], check=True)
    settings = Settings(tmp_dir=tmp_path / "tmp", rebuild_mode="extract")
    assert rebuild.image_owners(settings, image) == {(0, 0)}
    log = []
    rebuild.rebuild(settings, image, Changes(write={"autorun.cmd": b"CMD=game.exe\r\n"}),
                    Job(on_log=log.append))
    assert rebuild.image_owners(settings, image) == {(os.getuid(), os.getgid())}
    assert any(line.startswith("Propriétaire : 0:0 →") for line in log)


@needs_tools
def test_pseudo_options_add_tree_and_links_without_writing_source(tmp_path: Path):
    """Mode overlay : arborescence et liens ajoutés par mksquashfs (pseudo),
    la vue fuse-overlayfs n'est pas modifiée. Simulé ici sur un dossier."""
    from app.services.rebuild import Changes, pseudo_options, tree_entries
    src = tmp_path / "src"
    (src / "runners/D/files/lib").mkdir(parents=True)
    (src / "runners/D/files/lib/old.dll").write_bytes(b"old")
    (src / "sys").mkdir()
    (src / "sys/a b.dll").symlink_to("/ailleurs/a b.dll")
    tree = tmp_path / "tree"
    (tree / "icu/x64").mkdir(parents=True)
    (tree / "icu/x64/ic u.dll").write_bytes(b"icu" * 1000)
    (tree / "icu/x64/lien.dll").symlink_to("ic u.dll")
    (tree / "old.dll").write_bytes(b"new")                     # remplace un fichier existant
    changes = Changes(trees={"runners/D/files/lib": tree},
                      links={"sys/a b.dll": "../runners/D/files/lib/icu/x64/ic u.dll"})
    existing = {rel for rel, _ in tree_entries(src)}
    opts = pseudo_options(changes, existing, tmp_path, os.getuid(), os.getgid())
    out = tmp_path / "out.sqfs"
    subprocess.run(["mksquashfs", str(src), str(out), "-noappend", "-quiet", "-no-progress", *opts],
                   check=True, capture_output=True)
    assert _cat(out, "runners/D/files/lib/icu/x64/ic u.dll") == b"icu" * 1000
    assert _cat(out, "runners/D/files/lib/old.dll") == b"new"
    lls = subprocess.run(["unsquashfs", "-lls", str(out)], capture_output=True, check=True).stdout.decode()
    assert "squashfs-root/sys/a b.dll -> ../runners/D/files/lib/icu/x64/ic u.dll" in lls
    assert "squashfs-root/runners/D/files/lib/icu/x64/lien.dll -> ic u.dll" in lls
    assert (src / "sys/a b.dll").readlink().as_posix() == "/ailleurs/a b.dll"    # source intacte
    assert expected_listing(sorted(existing), changes) == _listing(out)


def test_run_progress_refuses_unreadable_files(tmp_path: Path):
    """mksquashfs continue (code 0) quand un fichier est illisible (montage
    FUSE mort) : l'image serait incomplète, la tâche doit échouer."""
    script = tmp_path / "p.sh"
    script.write_text("#!/bin/sh\necho 10\n"
                      "echo 'Failed to read file /m/system.reg, creating empty file'\n"
                      "echo 'Cannot stat dir/file /m/dosdevices because Transport endpoint is "
                      "not connected, ignoring'\necho 100\n")
    script.chmod(0o755)
    with pytest.raises(RebuildError, match="2 lecture.*system.reg"):
        rebuild.run_progress([str(script)], Job(), "mksquashfs", 0, 1)


@needs_tools
def test_pseudo_options_equivalent_to_apply_changes(tmp_path: Path):
    """Mode sans extraction : mksquashfs (exclusions + pseudo) doit produire
    exactement ce que donnent les changements appliqués sur une copie."""
    from app.services.rebuild import Changes, apply_changes, pseudo_options, tree_entries
    src = tmp_path / "src"
    (src / "Game Dir/sub").mkdir(parents=True)
    (src / "Game Dir/game.exe").write_bytes(b"MZ" * 100)
    (src / "Game Dir/old.dll").write_bytes(b"old")
    (src / "Game Dir/sub/x.dat").write_bytes(b"x")
    (src / "autorun.cmd").write_bytes(b"CMD=game.exe\r\n")
    (src / "pfx").symlink_to(".")
    (src / "lien.dll").symlink_to("/ailleurs/lien.dll")
    patch = tmp_path / "patch.bin"
    patch.write_bytes(b"patch" * 50)
    tree = tmp_path / "tree"
    (tree / "files/lib").mkdir(parents=True)
    (tree / "files/lib/a.dll").write_bytes(b"a")
    (tree / "files/lib/a.so").symlink_to("a.dll")
    changes = Changes(
        write={"autorun.cmd": b"CMD=game.exe\r\nPROTON=P\r\n", "Game Dir/new.ini": b"[x]"},
        copy={"Game Dir/old.dll": patch, "Nouveau/d/patch.bin": patch},
        delete=["Game Dir/sub"],
        trees={"runners/P": tree},
        links={"lien.dll": "runners/P/files/lib/a.dll"})
    existing = {rel for rel, _ in tree_entries(src)}

    ref_root = tmp_path / "ref"
    shutil.copytree(src, ref_root, symlinks=True)
    apply_changes(ref_root, changes)
    ref = tmp_path / "ref.sqfs"
    subprocess.run(["mksquashfs", str(ref_root), str(ref), "-noappend", "-quiet", "-no-progress"],
                   check=True, capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path / "out.sqfs"
    subprocess.run(["mksquashfs", str(src), str(out), "-noappend", "-quiet", "-no-progress",
                    *pseudo_options(changes, existing, work, os.getuid(), os.getgid())],
                   check=True, capture_output=True)

    def entries(image: Path) -> dict[str, str]:
        lls = subprocess.run(["unsquashfs", "-lls", str(image)], capture_output=True, text=True,
                             check=True).stdout
        out = {}
        for line in lls.splitlines():
            if "squashfs-root/" in line:
                rel = line.split("squashfs-root/", 1)[1]
                out[rel.split(" -> ")[0]] = line[0] + (rel.split(" -> ")[1] if " -> " in rel else "")
        return out
    assert entries(out) == entries(ref)
    for rel, kind in entries(ref).items():
        if kind == "-":
            assert _cat(out, rel) == _cat(ref, rel), rel
    assert expected_listing(sorted(existing), changes) == _listing(out)
