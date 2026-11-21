"""Éditeur en place (services/sqedit.py) : vraies images fabriquées par mksquashfs, relues par
unsquashfs. Chaque cas compare l'image éditée au dossier de référence modifié de la même façon."""

from __future__ import annotations

import hashlib
import os
import random
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from app.services import sqedit
from app.services.errors import ManagerError
from app.services.rebuild import Changes, apply_changes
from app.services.sqedit import Unsupported

needs_tools = pytest.mark.skipif(
    not (shutil.which("mksquashfs") and shutil.which("unsquashfs")),
    reason="squashfs-tools absent",
)
pytestmark = needs_tools


# ------------------------------------------------------------------ référence

def _build_reference(root: Path) -> None:
    """Dossier de référence : xattrs, petits fichiers (fragments), fichier de plusieurs blocs,
    fichiers vides, dossier de plus de 256 entrées (et un de plus de 64 Kio de liste : dossier
    étendu), sous-dossiers profonds, liens symboliques."""
    rng = random.Random(7)

    def put(rel: str, data: bytes) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    put("docs/readme.txt", b"lisez-moi\n")
    put("docs/b.txt", b"b" * 3000)
    put("xa.txt", b"avec attribut\n")
    os.setxattr(root / "xa.txt", "user.DOSATTRIB", b"0x20")
    os.setxattr(root / "docs", "user.dossier", b"garde")        # dossier étendu avec xattr
    put("gone.txt", b"a supprimer\n")
    put("olddir/a.txt", b"a")
    put("olddir/b/c.txt", b"c" * 500)
    put("big/blob.bin", rng.randbytes(300_000))
    put("empty.txt", b"")
    put("deep/a/b/c/d/e/f.txt", b"profond\n")
    for i in range(300):
        put(f"mid/fichier-{i:04d}.txt", f"{i}\n".encode())
    for i in range(3000):
        put(f"wide/un-nom-de-fichier-assez-long-{i:05d}", b"x")
    os.symlink("docs/readme.txt", root / "lnk")
    os.symlink("../xa.txt", root / "docs/rel")
    os.symlink("deep", root / "dirlnk")


@pytest.fixture(scope="module")
def reference(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("sqref") / "ref"
    root.mkdir()
    _build_reference(root)
    return root


def _mksquashfs(src: Path, image: Path, *opts: str) -> None:
    subprocess.run(["mksquashfs", str(src), str(image), "-noappend", "-quiet", "-no-progress",
                    *opts], check=True, capture_output=True)


VARIANTS = {
    "gzip": ["-comp", "gzip"],
    "xz": ["-comp", "xz"],
    "zstd": ["-comp", "zstd"],
    "zstd-sans-fragments": ["-comp", "zstd", "-no-fragments"],
    "zstd-sans-export": ["-comp", "zstd", "-no-exports"],
    "xz-sans-fragments-ni-export": ["-comp", "xz", "-no-fragments", "-no-exports"],
    "gzip-petits-blocs": ["-comp", "gzip", "-b", "4096", "-always-use-fragments"],
}


def _changes(tmp_path: Path) -> Changes:
    extra = tmp_path / "source-copiee.txt"
    extra.write_bytes(b"copie\n")
    extra.chmod(0o640)
    return Changes(
        write={"docs/readme.txt": b"nouveau lisez-moi\n",           # remplacé
               "xa.txt": b"remplace, attribut garde\n",              # remplacé avec xattr
               "big/blob.bin": random.Random(3).randbytes(400_000),  # plusieurs blocs
               "new/sub/deep/added.bin": b"ajout\n" * 100,           # nouveaux sous-dossiers
               "empty.txt": b"plus vide\n",
               "mid/fichier-0150.txt": b"remplace dans un gros dossier\n",
               "mid/zz-nouveau.txt": b"nouveau dans un gros dossier\n",
               "wide/un-nom-de-fichier-assez-long-01500": b"remplace dans un dossier etendu\n",
               "wide/zz-nouveau": b"nouveau dans un dossier etendu\n"},
        copy={"deep/a/b/c/d/copie.txt": extra},
        delete=["gone.txt", "olddir", "mid/fichier-0007.txt", "absent/inconnu"],
        links={"lnk": "docs/b.txt", "new/lnk2": "../xa.txt"},
    )


def _apply_to_reference(ref: Path, changes: Changes) -> None:
    """Mêmes changements sur le dossier de référence ; les attributs d'un fichier remplacé sont
    gardés, comme le fait l'éditeur."""
    saved = {}
    for rel in [*changes.write, *changes.copy]:
        path = ref / rel
        if path.is_file() and not path.is_symlink():
            saved[rel] = {n: os.getxattr(path, n) for n in os.listxattr(path)}
    apply_changes(ref, changes)
    for rel, attrs in saved.items():
        for name, value in attrs.items():
            os.setxattr(ref / rel, name, value)


# ------------------------------------------------------------------ comparaison

def _tree(root: Path) -> dict[str, tuple]:
    out = {}
    for cur, dirs, files in os.walk(root):
        for name in [*dirs, *files]:
            path = Path(cur) / name
            rel = path.relative_to(root).as_posix()
            attrs = {n: os.getxattr(path, n, follow_symlinks=False)
                     for n in os.listxattr(path, follow_symlinks=False) if n.startswith("user.")}
            if path.is_symlink():
                out[rel] = ("l", os.readlink(path), attrs)
            elif path.is_dir():
                out[rel] = ("d", None, attrs)
            else:
                out[rel] = ("-", hashlib.sha256(path.read_bytes()).hexdigest(), attrs)
    return out


def _unsquash(image: Path, dest: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["unsquashfs", "-q", "-d", str(dest), str(image)],
                          capture_output=True, check=True)


def _lls(image: Path) -> dict[str, tuple[str, int]]:
    out = subprocess.run(["unsquashfs", "-lls", str(image)], capture_output=True,
                         check=True).stdout.decode("utf-8", "surrogateescape")
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6 or not parts[5].startswith("squashfs-root/"):
            continue
        name = parts[5][len("squashfs-root/"):]
        if parts[0][0] == "l":
            name = name.split(" -> ")[0]
        table[name] = (parts[0][0], int(parts[2]))
    return table


def _check(image: Path, ref: Path, tmp_path: Path, label: str = "out") -> None:
    """Image == référence : arborescence, types, tailles, liens, octets, xattrs ; table lisible."""
    stat = subprocess.run(["unsquashfs", "-s", str(image)], capture_output=True, check=False)
    assert stat.returncode == 0, stat.stderr
    out = tmp_path / label
    shutil.rmtree(out, ignore_errors=True)
    done = _unsquash(image, out)
    assert not done.stderr.strip(), done.stderr
    assert _tree(out) == _tree(ref)
    table = _lls(image)
    for rel, (kind, target_or_hash, _) in _tree(ref).items():
        path = ref / rel
        assert table[rel][0] == kind, rel
        if kind == "-":
            assert table[rel][1] == path.stat().st_size, rel
        elif kind == "l":
            assert table[rel][1] == len(os.readlink(path)), rel
    assert set(table) == set(_tree(ref))


def _layout(image: Path) -> None:
    """Disposition standard de mksquashfs, dont dépend le noyau (il borne chaque index par la
    table suivante) : inodes, dossiers, [fragments], [export], ids, [xattrs]."""
    raw = image.read_bytes()
    f = struct.unpack("<IIIIIHHHHHHQQQQQQQQ", raw[:96])
    frags, no_ids = f[4], f[8]
    used, id_start, xattr_start, it, dt, fr, lookup = f[12:19]
    assert 96 < it < dt <= fr < id_start < used and f[7] & sqedit.FLAG_EXPORTABLE == 0
    assert lookup == sqedit.INVALID and used <= len(raw)
    assert len(raw) % 4096 == 0 and len(raw) - used < 4096
    id_ptrs = struct.unpack_from("<Q", raw, id_start)[0]
    if frags:
        assert fr + 8 * -(-frags * 16 // 8192) == id_ptrs        # l'index finit où les ids commencent
    index_end = id_start + 8 * -(-no_ids * 4 // 8192)
    if xattr_start == sqedit.INVALID:
        assert index_end == used
    else:
        kv, ids, _ = struct.unpack_from("<QII", raw, xattr_start)
        assert index_end == kv and xattr_start + 16 + 8 * -(-ids * 16 // 8192) == used


@pytest.fixture
def make(tmp_path: Path, reference: Path):
    def build(variant: str) -> tuple[Path, Path]:
        ref = tmp_path / "ref"
        shutil.copytree(reference, ref, symlinks=True)
        image = tmp_path / "Jeu.wsquashfs"
        _mksquashfs(ref, image, *VARIANTS[variant])
        return image, ref
    return build


SB = 96


def _sha(path: Path, size: int | None = None) -> str:
    data = path.read_bytes()
    return hashlib.sha256(data if size is None else data[:size]).hexdigest()


# ------------------------------------------------------------------ éditions

@pytest.mark.parametrize("variant", list(VARIANTS))
def test_edit_matches_reference(variant: str, make, tmp_path: Path):
    image, ref = make(variant)
    old_size = image.stat().st_size
    changes = _changes(tmp_path)

    result = sqedit.edit(image, changes)

    _apply_to_reference(ref, changes)
    _check(image, ref, tmp_path)
    assert result.old_size == old_size and result.new_size == image.stat().st_size
    assert result.new_size % 4096 == 0 and result.new_size > old_size
    assert result.backup == sqedit.sb_old_path(image) and result.backup.exists()
    assert result.dead_bytes > 0 and sqedit.dead_bytes(image) == result.dead_bytes
    _layout(image)


def test_edit_rewrites_nothing_but_superblock(make, tmp_path: Path):
    image, _ = make("zstd")
    before = image.read_bytes()
    sqedit.edit(image, _changes(tmp_path))
    after = image.read_bytes()
    assert after[SB:len(before)] == before[SB:]
    assert after[:SB] != before[:SB]


def test_edit_keeps_owner_mode_and_xattr(make, tmp_path: Path):
    image, ref = make("zstd")
    sqedit.edit(image, Changes(write={"xa.txt": b"neuf\n", "docs/neuf.txt": b"n\n"}))
    out = tmp_path / "out"
    _unsquash(image, out)
    assert os.getxattr(out / "xa.txt", "user.DOSATTRIB") == b"0x20"
    assert (out / "xa.txt").stat().st_mode & 0o777 == (ref / "xa.txt").stat().st_mode & 0o777
    assert (out / "docs/neuf.txt").stat().st_mode & 0o777 == 0o644
    lln = subprocess.run(["unsquashfs", "-lln", str(image)], capture_output=True, check=True)
    owners = {line.split()[1] for line in lln.stdout.decode().splitlines()[1:] if "/" in line}
    assert len(owners) == 1                       # le nouveau fichier prend l'uid/gid du dossier


def test_copy_takes_source_mode(make, tmp_path: Path):
    image, _ = make("zstd")
    source = tmp_path / "s.bin"
    source.write_bytes(b"x" * 10)
    source.chmod(0o750)
    sqedit.edit(image, Changes(copy={"docs/s.bin": source}))
    out = tmp_path / "out"
    _unsquash(image, out)
    assert (out / "docs/s.bin").stat().st_mode & 0o777 == 0o750


def test_successive_edits_then_undo(make, tmp_path: Path):
    image, ref = make("zstd")
    steps = [
        Changes(write={"docs/readme.txt": b"un\n", "a/b/c.txt": b"c1\n"}),
        Changes(write={"a/b/c.txt": b"c2\n", "a/d.txt": b"d\n"}, delete=["gone.txt"]),
        Changes(links={"lnk": "deep"}, delete=["a/b"], write={"deep/a/x": b"x"}),
    ]
    dead = []
    for i, changes in enumerate(steps):
        result = sqedit.edit(image, changes)
        _apply_to_reference(ref, changes)
        _check(image, ref, tmp_path, f"out{i}")
        dead.append(result.dead_bytes)
        # Tant qu'une édition n'est pas validée, une autre est refusée
        with pytest.raises(Unsupported):
            sqedit.edit(image, Changes(write={"z": b"z"}))
        sqedit.commit(image)
        assert not sqedit.sb_old_path(image).exists()
    assert dead == sorted(dead) and dead[0] < dead[-1]           # cumulé

    before = image.read_bytes()
    dead_before = sqedit.dead_bytes(image)
    sqedit.edit(image, Changes(write={"docs/readme.txt": b"quatre\n", "n/e.txt": b"e"}))
    assert sqedit.dead_bytes(image) > dead_before
    sqedit.undo(image)
    assert image.read_bytes() == before
    assert not sqedit.sb_old_path(image).exists()
    assert sqedit.dead_bytes(image) == dead_before
    _check(image, ref, tmp_path, "apres-undo")


def test_undo_restores_original_after_one_edit(make, tmp_path: Path):
    image, ref = make("xz")
    before = _sha(image)
    sqedit.edit(image, _changes(tmp_path))
    sqedit.undo(image)
    assert _sha(image) == before
    assert sqedit.dead_bytes(image) == 0 and not sqedit.dead_path(image).exists()
    _check(image, ref, tmp_path)


def test_undo_without_backup_or_with_short_file(make):
    image, _ = make("zstd")
    with pytest.raises(ManagerError):
        sqedit.undo(image)
    with pytest.raises(ManagerError):
        sqedit.commit(image)
    sqedit.edit(image, Changes(write={"docs/readme.txt": b"x"}))
    with open(image, "r+b") as f:
        f.truncate(4096)
    with pytest.raises(ManagerError, match="plus courte"):
        sqedit.undo(image)
    assert sqedit.sb_old_path(image).exists()


def test_interrupted_before_superblock_leaves_image_untouched(make, tmp_path: Path, monkeypatch):
    image, ref = make("zstd")
    before = image.read_bytes()

    def boom(fd, raw):
        raise OSError("coupure simulée")

    monkeypatch.setattr(sqedit, "_write_superblock", boom)
    with pytest.raises(OSError, match="coupure"):
        sqedit.edit(image, _changes(tmp_path))
    assert image.read_bytes() == before          # même la taille : fichier retronqué
    assert not sqedit.sb_old_path(image).exists() and not sqedit.dead_path(image).exists()
    _check(image, ref, tmp_path)


def test_crash_leaves_old_state_readable(make, tmp_path: Path, monkeypatch):
    """Arrêt brutal (rien ne nettoie) avant le superbloc : l'image lit comme avant sur [0, taille
    d'origine) ; l'espace ajouté derrière est ignoré (bytes_used)."""
    image, ref = make("zstd")
    before = image.read_bytes()

    def crash(fd, raw):
        raise KeyboardInterrupt

    monkeypatch.setattr(sqedit, "_rollback", lambda *a: None)
    monkeypatch.setattr(sqedit, "_write_superblock", crash)
    with pytest.raises(KeyboardInterrupt):
        sqedit.edit(image, _changes(tmp_path))
    after = image.read_bytes()
    assert after[:len(before)] == before and len(after) > len(before)
    _check(image, ref, tmp_path)
    sqedit.undo(image)                           # la sauvegarde subsiste : undo rétablit
    assert image.read_bytes() == before


def test_in_use_game_refused_and_image_untouched(make, tmp_path: Path):
    image, _ = make("zstd")
    before = image.read_bytes()
    with pytest.raises(ManagerError, match="jeu lancé"):
        sqedit.edit(image, _changes(tmp_path), in_use=lambda p: True)
    assert image.read_bytes() == before and not sqedit.sb_old_path(image).exists()


def test_noop_is_refused_without_touching(make):
    image, _ = make("zstd")
    before = image.read_bytes()
    with pytest.raises(Unsupported):
        sqedit.edit(image, Changes(delete=["absent"]))
    assert image.read_bytes() == before


# ------------------------------------------------------------------ refus

def test_refuses_trees(make, tmp_path: Path):
    image, _ = make("zstd")
    (tmp_path / "t").mkdir()
    with pytest.raises(Unsupported, match="arborescence"):
        sqedit.edit(image, Changes(trees={"runners/x": tmp_path / "t"}))


def test_refuses_more_than_64_mib(make):
    image, _ = make("zstd")
    before = image.read_bytes()
    with pytest.raises(Unsupported, match="Mio"):
        sqedit.edit(image, Changes(write={"gros.bin": bytes(65 << 20)}))
    assert image.read_bytes() == before


def test_refuses_unread_compression(make):
    image, _ = make("zstd")
    raw = bytearray(image.read_bytes())
    struct.pack_into("<H", raw, 20, 3)                    # lzo : non lu
    image.write_bytes(raw)
    with pytest.raises(Unsupported, match="compression"):
        sqedit.edit(image, Changes(write={"a": b"a"}))
    struct.pack_into("<H", raw, 20, 5)                    # lz4
    image.write_bytes(raw)
    with pytest.raises(Unsupported, match="compression"):
        sqedit.edit(image, Changes(write={"a": b"a"}))


def test_refuses_not_squashfs(tmp_path: Path):
    image = tmp_path / "x.wsquashfs"
    image.write_bytes(b"\0" * 8192)
    with pytest.raises(Unsupported):
        sqedit.edit(image, Changes(write={"a": b"a"}))


def test_refuses_hard_linked_file(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a").write_bytes(b"A" * 1000)
    os.link(src / "a", src / "b")
    (src / "c").write_bytes(b"c")
    image = tmp_path / "h.wsquashfs"
    _mksquashfs(src, image, "-comp", "zstd")
    before = image.read_bytes()
    for changes in (Changes(write={"a": b"neuf"}), Changes(delete=["b"]),
                    Changes(links={"b": "c"})):
        with pytest.raises(Unsupported, match="liens physiques"):
            sqedit.edit(image, changes)
    assert image.read_bytes() == before
    sqedit.edit(image, Changes(write={"c": b"autre"}))    # le reste reste éditable
    out = tmp_path / "out"
    _unsquash(image, out)
    assert (out / "c").read_bytes() == b"autre" and (out / "a").read_bytes() == b"A" * 1000


def test_refuses_unknown_inode_type(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    os.mkfifo(src / "tube")
    (src / "f").write_bytes(b"f")
    image = tmp_path / "s.wsquashfs"
    _mksquashfs(src, image, "-comp", "zstd")
    with pytest.raises(Unsupported, match="inode"):
        sqedit.edit(image, Changes(write={"tube": b"x"}))
    with pytest.raises(Unsupported, match="inode"):
        sqedit.edit(image, Changes(delete=["tube"]))
    sqedit.edit(image, Changes(write={"f": b"g"}))        # un tube intact ne gêne pas
    out = tmp_path / "out"
    _unsquash(image, out)
    assert (out / "f").read_bytes() == b"g" and (out / "tube").exists()


def test_refuses_file_over_directory_and_through_file(make):
    image, _ = make("zstd")
    with pytest.raises(Unsupported, match="dossier"):
        sqedit.edit(image, Changes(write={"docs": b"x"}))
    with pytest.raises(Unsupported, match="fichier"):
        sqedit.edit(image, Changes(write={"xa.txt/sous": b"x"}))


def test_refuses_when_backup_present(make):
    image, _ = make("zstd")
    old = image.with_name(image.name + ".old")
    old.write_bytes(b"")
    with pytest.raises(Unsupported, match="sauvegarde"):
        sqedit.edit(image, Changes(write={"a": b"a"}))
    old.unlink()
    sqedit.sb_old_path(image).write_text("{}")
    with pytest.raises(Unsupported, match="sauvegarde"):
        sqedit.edit(image, Changes(write={"a": b"a"}))


def test_refuses_unreadable_metadata_and_unknown_layout(make):
    image, _ = make("zstd")
    raw = bytearray(image.read_bytes())
    it = struct.unpack_from("<Q", raw, 64)[0]
    root = struct.unpack_from("<Q", raw, 32)[0] >> 16     # bloc d'inodes de la racine, corrompu
    for i in range(2, 12):
        raw[it + root + i] ^= 0xFF
    image.write_bytes(raw)
    with pytest.raises(Unsupported):
        sqedit.edit(image, Changes(write={"a": b"a"}))
