"""Éditeur en place des images ``.wsquashfs`` : petites retouches en quelques secondes.

Remplacer une illustration, l'autorun, quelques fichiers ou des liens passe sinon par
`rebuild()`, qui décompresse et recompresse toute l'image (15 à 21 min pour 100 Go, avec une
copie ``.old`` complète). Une image squashfs est pourtant [superbloc 96 o][données][tables] :
les données sont adressées par position absolue et le superbloc donne la position des tables.
Cet éditeur, en Python pur, ne réécrit donc rien d'existant sauf le superbloc :

1. les nouvelles données sont écrites à la suite du fichier (sans compression) ;
2. de nouvelles tables sont écrites derrière, dans l'ordre standard de mksquashfs (le noyau et
   unsquashfs bornent chaque table par la suivante) : table des inodes d'origine copiée octet
   pour octet puis les nouveaux blocs ; table des dossiers de même ; fragments, ids, xattrs
   recopiés, leurs index (positions absolues) décalés. Les références internes (inode, dossier,
   xattr) sont relatives au début de leur table : la copie à l'identique les garde valides.
   Seuls sont recréés l'inode du fichier (ou du lien) et les inodes et listes des dossiers
   parents jusqu'à la racine ;
3. le superbloc, écrit en dernier (96 octets, ``fsync``), fait basculer l'image. L'ancien est
   gardé dans ``<image>.sb-old`` : « valider » (`commit`) le supprime, « restaurer » (`undo`)
   le remet et retronque le fichier. Avant cette bascule, une erreur remet le fichier dans
   son état d'origine.

Choix de conception :

- **Table d'export (NFS)** : supprimée (``lookup_table_start`` invalide, drapeau EXPORTABLE
  retiré). La réécrire demanderait la référence de chaque inode de l'image, y compris des
  numéros disparus, pour un usage (partage NFS de l'image montée) que ni le lanceur ni Batocera
  n'ont ; la supprimer est valide pour le noyau comme pour unsquashfs.
- **Numérotation** : un inode modifié garde son numéro (le ``parent_inode`` des sous-dossiers
  reste juste), un nouveau prend la suite du compteur du superbloc. Un fichier supprimé laisse
  un trou, sans conséquence en lecture (le noyau et unsquashfs n'exigent une numérotation
  dense que pour l'export, supprimé).
- **Espace perdu** : anciennes tables et données remplacées ou supprimées ne sont pas
  récupérées ; leur total approximatif, cumulé d'une édition à l'autre, est tenu dans
  ``<image>.sqedit`` (JSON, à côté de l'image, jamais dedans). Une reconstruction complète
  repart à zéro (`reset_dead`).

Tout ce dont l'éditeur n'est pas sûr lève `Unsupported` : l'appelant retombe alors sur la
reconstruction complète."""

from __future__ import annotations

import json
import lzma
import os
import struct
import time
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .durable import fsync_dir, write_atomic
from .errors import ManagerError
from .squashfs import safe_rel

if TYPE_CHECKING:
    from .rebuild import Changes

MAGIC = 0x73717368
_SB = struct.Struct("<IIIIIHHHHHHQQQQQQQQ")
SB_SIZE = _SB.size                      # 96
INVALID = 0xFFFFFFFFFFFFFFFF            # table absente (superbloc)
NO_XATTR = 0xFFFFFFFF
NO_FRAGMENT = 0xFFFFFFFF
META_SIZE = 8192                        # octets décompressés d'un bloc de métadonnées
META_RAW = 0x8000                       # en-tête de bloc de métadonnées : non compressé
DATA_RAW = 1 << 24                      # taille d'un bloc de données : non compressé
MAX_NEW_DATA = 64 << 20                 # au-delà, reconstruction complète
FLAG_EXPORTABLE = 0x80
ALIGN = 4096
COMPRESSORS = {1: "gzip", 2: "lzma", 4: "xz", 6: "zstd"}

# Types d'inode : 1 dossier, 2 fichier, 3 lien, 4-7 spéciaux ; 8-14 leurs variantes étendues
T_DIR, T_REG, T_LNK, T_LDIR, T_LREG, T_LLNK = 1, 2, 3, 8, 9, 10


class Unsupported(ManagerError):
    """L'éditeur en place refuse : l'appelant reconstruit l'image en entier."""


@dataclass
class EditResult:
    backup: Path          # <image>.sb-old
    old_size: int         # taille du fichier avant
    new_size: int         # après
    dead_bytes: int       # espace perdu cumulé (tables et données remplacées)


# ------------------------------------------------------------------ fichiers annexes

def sb_old_path(image: Path) -> Path:
    return image.with_name(image.name + ".sb-old")


def dead_path(image: Path) -> Path:
    return image.with_name(image.name + ".sqedit")


def dead_bytes(image: Path) -> int:
    """Espace perdu cumulé par les éditions en place (0 si l'image n'en a pas subi)."""
    try:
        return int(json.loads(dead_path(image).read_text(encoding="utf-8")).get("dead_bytes", 0))
    except (OSError, ValueError, AttributeError):
        return 0


def _set_dead(image: Path, value: int) -> None:
    if value > 0:
        write_atomic(dead_path(image), json.dumps({"dead_bytes": value}))
    else:
        dead_path(image).unlink(missing_ok=True)


def reset_dead(image: Path) -> None:
    """Après une reconstruction complète : l'image est de nouveau compacte."""
    dead_path(image).unlink(missing_ok=True)


# ------------------------------------------------------------------ superbloc

@dataclass
class _Superblock:
    magic: int
    inodes: int
    mkfs_time: int
    block_size: int
    fragments: int
    compression: int
    block_log: int
    flags: int
    no_ids: int
    major: int
    minor: int
    root_inode: int
    bytes_used: int
    id_table_start: int
    xattr_id_table_start: int
    inode_table_start: int
    directory_table_start: int
    fragment_table_start: int
    lookup_table_start: int

    @classmethod
    def parse(cls, raw: bytes) -> _Superblock:
        if len(raw) < SB_SIZE:
            raise Unsupported("superbloc tronqué")
        sb = cls(*_SB.unpack(raw[:SB_SIZE]))
        if sb.magic != MAGIC or (sb.major, sb.minor) != (4, 0):
            raise Unsupported("ce n'est pas une image squashfs 4.0")
        if sb.compression not in COMPRESSORS:
            raise Unsupported(f"compression {sb.compression} non lue par l'éditeur en place")
        if sb.block_size != 1 << sb.block_log or not 4096 <= sb.block_size <= 1 << 20:
            raise Unsupported("taille de bloc incohérente")
        return sb

    def pack(self) -> bytes:
        return _SB.pack(*(getattr(self, name) for name in self.__dataclass_fields__))


def _write_superblock(fd: int, raw: bytes) -> None:
    """Bascule de l'image (96 octets à l'offset 0) ; point d'injection des tests."""
    _pwrite(fd, raw, 0)


def _pwrite(fd: int, data: bytes | bytearray | memoryview, offset: int) -> None:
    view = memoryview(data)
    while view:
        done = os.pwrite(fd, view, offset)
        view, offset = view[done:], offset + done


def _pread(fd: int, size: int, offset: int) -> bytes:
    data = os.pread(fd, size, offset)
    if len(data) != size:
        raise Unsupported(f"lecture hors de l'image (position {offset}, {size} octets)")
    return data


# ------------------------------------------------------------------ métadonnées

def _decompress(compression: int, data: bytes) -> bytes:
    try:
        if compression == 1:
            return zlib.decompress(data)
        if compression == 2:
            return lzma.decompress(data, format=lzma.FORMAT_ALONE)
        if compression == 4:
            return lzma.decompress(data)
        import zstandard  # dépendance chargée à la demande : absente, l'éditeur refuse
        return zstandard.ZstdDecompressor().decompress(data, max_output_size=META_SIZE)
    except ImportError as exc:
        raise Unsupported("module zstandard absent") from exc
    except Exception as exc:   # zlib.error, LZMAError, ZstdError : bloc illisible
        raise Unsupported(f"bloc de métadonnées illisible ({exc})") from exc


class _Meta:
    """Table de blocs de métadonnées (inodes ou dossiers) : lecture à travers les blocs,
    positions relatives au début de la table (comme les références internes)."""

    def __init__(self, fd: int, start: int, length: int, compression: int):
        self.fd, self.start, self.length, self.compression = fd, start, length, compression
        self._cache: dict[int, tuple[bytes, int]] = {}

    def block(self, rel: int) -> tuple[bytes, int]:
        """(contenu décompressé, position relative du bloc suivant)."""
        got = self._cache.get(rel)
        if got is None:
            if not 0 <= rel < self.length:
                raise Unsupported("référence hors de la table")
            header = struct.unpack("<H", _pread(self.fd, 2, self.start + rel))[0]
            size = header & 0x7FFF
            if size == 0 or size > META_SIZE + 512:
                raise Unsupported("bloc de métadonnées invalide")
            raw = _pread(self.fd, size, self.start + rel + 2)
            data = raw if header & META_RAW else _decompress(self.compression, raw)
            got = self._cache[rel] = (data, rel + 2 + size)
        return got

    def read(self, rel: int, offset: int, size: int) -> bytes:
        out = bytearray()
        while len(out) < size:
            data, following = self.block(rel)
            if offset >= len(data):             # le décalage déborde sur les blocs suivants
                rel, offset = following, offset - len(data)
                continue
            out += data[offset:offset + size - len(out)]
            rel, offset = following, 0
        return bytes(out)


def _chain(fd: int, start: int, stop: int) -> list[int]:
    """Positions absolues des blocs de métadonnées contigus de `start` jusqu'à `stop`, qui
    doit tomber pile sur la fin d'un bloc."""
    out = []
    pos = start
    while pos < stop:
        out.append(pos)
        size = struct.unpack("<H", _pread(fd, 2, pos))[0] & 0x7FFF
        if size == 0:
            raise Unsupported("bloc de métadonnées invalide")
        pos += 2 + size
    if pos != stop:
        raise Unsupported("tables non contiguës : disposition inhabituelle")
    return out


# ------------------------------------------------------------------ inodes

@dataclass
class _Inode:
    kind: int
    mode: int
    uid: int
    gid: int
    mtime: int
    number: int
    nlink: int = 1
    xattr: int = NO_XATTR
    start: int = 0              # dossier : bloc de la liste ; fichier : début des données
    offset: int = 0
    size: int = 0               # dossier : file_size ; fichier : taille ; lien : longueur
    parent: int = 0
    fragment: int = NO_FRAGMENT
    blocks: list[int] = field(default_factory=list)
    target: bytes = b""


# Taille de ce qui suit l'en-tête de 16 octets, avant les parties variables
_FIXED = {T_DIR: 16, T_LDIR: 24, T_REG: 16, T_LREG: 40, T_LNK: 8, T_LLNK: 8}


@dataclass
class _Entry:
    kind: int                   # type basique de l'entrée de liste (1 dossier, 2 fichier, 3 lien…)
    number: int
    block: int                  # bloc de l'inode, relatif à la table des inodes
    offset: int


class _Dir:
    """Dossier chargé : ses entrées, modifiables, et son inode d'origine (None : nouveau)."""

    def __init__(self, inode: _Inode | None, entries: dict[bytes, _Entry], parent: _Dir | None,
                 number: int, uid: int, gid: int, mode: int):
        self.inode, self.entries, self.parent = inode, entries, parent
        self.number, self.uid, self.gid, self.mode = number, uid, gid, mode
        self.children: dict[bytes, _Dir] = {}
        self.dirty = False

    def touch(self) -> None:
        node: _Dir | None = self
        while node is not None and not node.dirty:
            node.dirty = True
            node = node.parent


class _Session:
    """Une édition : lecture de l'image d'origine, données et nouvelles tables en mémoire."""

    def __init__(self, fd: int, sb: _Superblock, old_size: int):
        self.fd, self.sb, self.old_size = fd, sb, old_size
        if sb.bytes_used > old_size:
            raise Unsupported("image tronquée (bytes_used dépasse la taille du fichier)")
        self._locate_tables()
        self.inodes = _Meta(fd, sb.inode_table_start, self.it_len, sb.compression)
        self.dirs = _Meta(fd, sb.directory_table_start, self.dt_len, sb.compression)
        self.pos = old_size                       # prochaine écriture de données
        self.new_inodes = bytearray()
        self.new_dirs = bytearray()
        self.next_number = sb.inodes + 1
        self.created = 0
        self.data_dead = 0                        # données remplacées ou supprimées
        self.now = int(time.time())
        self.root = self._load_dir(sb.root_inode >> 16, sb.root_inode & 0xFFFF, None)

    # ---- disposition de l'image d'origine

    def _locate_tables(self) -> None:
        sb, fd = self.sb, self.fd
        if not 0 < sb.inode_table_start < sb.directory_table_start <= sb.bytes_used:
            raise Unsupported("disposition des tables inhabituelle")
        self.it_len = sb.directory_table_start - sb.inode_table_start
        _chain(fd, sb.inode_table_start, sb.directory_table_start)
        # Début de chaque table qui suit celle des dossiers : elle s'arrête là
        following = {sb.fragment_table_start, sb.id_table_start}
        self.frag_ptrs = self._index(sb.fragment_table_start, sb.fragments * 16) if sb.fragments else []
        self.id_ptrs = self._index(sb.id_table_start, sb.no_ids * 4) if sb.no_ids else []
        if self.frag_ptrs:
            following.add(self.frag_ptrs[0])
        if self.id_ptrs:
            following.add(self.id_ptrs[0])
        self.xattr_ptrs: list[int] = []
        self.xattr_ids = 0
        self.xattr_kv = 0
        if sb.xattr_id_table_start != INVALID:
            if not sb.xattr_id_table_start + 16 <= sb.bytes_used:
                raise Unsupported("table des xattrs hors de l'image")
            self.xattr_kv, self.xattr_ids, _ = struct.unpack(
                "<QII", _pread(fd, 16, sb.xattr_id_table_start))
            if not self.xattr_ids:
                raise Unsupported("table des xattrs vide : disposition inhabituelle")
            self.xattr_ptrs = self._index(sb.xattr_id_table_start + 16, self.xattr_ids * 16)
            following |= {sb.xattr_id_table_start, self.xattr_kv}
        if sb.lookup_table_start != INVALID:
            following.add(sb.lookup_table_start)
            if sb.inodes:
                lookup = self._index(sb.lookup_table_start, sb.inodes * 8)
                following.add(lookup[0])
        following = {p for p in following if sb.directory_table_start < p <= sb.bytes_used}
        pos = sb.directory_table_start
        while pos not in following:
            if pos >= sb.bytes_used:
                raise Unsupported("fin de la table des dossiers introuvable")
            size = struct.unpack("<H", _pread(fd, 2, pos))[0] & 0x7FFF
            if size == 0:
                raise Unsupported("bloc de métadonnées invalide")
            pos += 2 + size
        self.dt_len = pos - sb.directory_table_start
        self.dir_table_end = pos
        # Tables indexées : leurs blocs doivent être contigus et finir sur l'index
        if self.frag_ptrs:
            self._check_run(self.frag_ptrs, sb.fragment_table_start)
        if self.id_ptrs:
            self._check_run(self.id_ptrs, sb.id_table_start)
        if self.xattr_ids:
            if not self.xattr_kv < self.xattr_ptrs[0] < sb.xattr_id_table_start:
                raise Unsupported("table des xattrs inhabituelle")
            _chain(fd, self.xattr_kv, sb.xattr_id_table_start)
            if sb.xattr_id_table_start + 16 + 8 * len(self.xattr_ptrs) != sb.bytes_used:
                raise Unsupported("la table des xattrs ne finit pas l'image")

    def _index(self, start: int, nbytes: int) -> list[int]:
        count = -(-nbytes // META_SIZE)
        return list(struct.unpack(f"<{count}Q", _pread(self.fd, 8 * count, start))) if count else []

    def _check_run(self, ptrs: list[int], index_start: int) -> None:
        if _chain(self.fd, ptrs[0], index_start) != ptrs:
            raise Unsupported("table indexée inhabituelle (blocs non contigus)")

    # ---- lecture

    def read_inode(self, block: int, offset: int) -> _Inode:
        kind, mode, uid, gid, mtime, number = struct.unpack("<HHHHII", self.inodes.read(block, offset, 16))
        if kind not in _FIXED:
            raise Unsupported(f"type d'inode {kind} non pris en charge")
        body = self.inodes.read(block, offset, 16 + _FIXED[kind])[16:]
        ino = _Inode(kind, mode, uid, gid, mtime, number)
        if kind == T_DIR:
            ino.start, ino.nlink, ino.size, ino.offset, ino.parent = struct.unpack("<IIHHI", body)
        elif kind == T_LDIR:
            (ino.nlink, ino.size, ino.start, ino.parent, _count, ino.offset,
             ino.xattr) = struct.unpack("<IIIIHHI", body)
        elif kind == T_REG:
            ino.start, ino.fragment, ino.offset, ino.size = struct.unpack("<IIII", body)
        elif kind == T_LREG:
            (ino.start, ino.size, _sparse, ino.nlink, ino.fragment, ino.offset,
             ino.xattr) = struct.unpack("<QQQIIII", body)
        else:
            ino.nlink, ino.size = struct.unpack("<II", body)
        if kind in (T_REG, T_LREG):
            bs = self.sb.block_size
            count = ino.size // bs if ino.fragment != NO_FRAGMENT else -(-ino.size // bs)
            raw = self.inodes.read(block, offset + 16 + _FIXED[kind], 4 * count)
            ino.blocks = list(struct.unpack(f"<{count}I", raw))
        elif kind in (T_LNK, T_LLNK):
            raw = self.inodes.read(block, offset, 24 + ino.size + (4 if kind == T_LLNK else 0))[24:]
            ino.target = raw[:ino.size]
            if kind == T_LLNK:
                ino.xattr = struct.unpack("<I", raw[ino.size:])[0]
        return ino

    def read_listing(self, ino: _Inode) -> dict[bytes, _Entry]:
        total = ino.size - 3
        if total <= 0:
            return {}
        data = self.dirs.read(ino.start, ino.offset, total)
        out: dict[bytes, _Entry] = {}
        pos = 0
        while pos < total:
            count, block, base = struct.unpack_from("<III", data, pos)
            pos += 12
            for _ in range(count + 1):
                offset, diff, kind, name_len = struct.unpack_from("<HhHH", data, pos)
                pos += 8
                name = data[pos:pos + name_len + 1]
                pos += name_len + 1
                if kind not in range(1, 15) or pos > total:
                    raise Unsupported("liste de dossier inhabituelle")
                out[name] = _Entry(kind, base + diff, block, offset)
        return out

    def _load_dir(self, block: int, offset: int, parent: _Dir | None) -> _Dir:
        ino = self.read_inode(block, offset)
        if ino.kind not in (T_DIR, T_LDIR):
            raise Unsupported("dossier attendu")
        return _Dir(ino, self.read_listing(ino), parent, ino.number, ino.uid, ino.gid, ino.mode)

    def _child_dir(self, parent: _Dir, name: bytes) -> _Dir | None:
        child = parent.children.get(name)
        if child is None:
            entry = parent.entries.get(name)
            if entry is None:
                return None
            if entry.kind != T_DIR:
                raise _NotADir
            child = parent.children[name] = self._load_dir(entry.block, entry.offset, parent)
        return child

    def _walk(self, parts: list[bytes], create: bool) -> _Dir | None:
        """Dossier de `parts` ; les manquants sont créés si `create`, sinon None."""
        node = self.root
        for name in parts:
            try:
                child = self._child_dir(node, name)
            except _NotADir:
                if create:
                    raise Unsupported("un composant du chemin est un fichier dans l'image") from None
                return None
            if child is None:
                if not create:
                    return None
                child = _Dir(None, {}, node, self._number(), node.uid, node.gid, 0o755)
                child.dirty = True
                node.children[name] = child
                node.entries[name] = _Entry(T_DIR, child.number, 0, 0)    # position fixée à l'écriture
                node.touch()
            node = child
        return node

    def _number(self) -> int:
        number = self.next_number
        self.next_number += 1
        self.created += 1
        return number

    # ---- écriture des nouvelles données et métadonnées

    def _emit_inode(self, raw: bytes) -> tuple[int, int]:
        at = len(self.new_inodes)
        self.new_inodes += raw
        return self.it_len + (at // META_SIZE) * (2 + META_SIZE), at % META_SIZE

    def _emit_listing(self, entries: list[tuple[bytes, _Entry]]) -> tuple[int, int, int]:
        """Liste de dossier triée par nom : (bloc, décalage, taille) dans la table des dossiers."""
        out = bytearray()
        i = 0
        while i < len(entries):
            first = entries[i][1]
            group = []
            for name, entry in entries[i:i + 256]:
                if entry.block != first.block or not -32768 <= entry.number - first.number <= 32767:
                    break
                group.append((name, entry))
            out += struct.pack("<III", len(group) - 1, first.block, first.number)
            for name, entry in group:
                out += struct.pack("<HhHH", entry.offset, entry.number - first.number, entry.kind,
                                   len(name) - 1) + name
            i += len(group)
        at = len(self.new_dirs)
        self.new_dirs += out
        return self.dt_len + (at // META_SIZE) * (2 + META_SIZE), at % META_SIZE, len(out)

    def _write_data(self, source: bytes | Path) -> tuple[int, int, list[int]]:
        """Données d'un fichier, à la suite et non compressées : (début, taille, tailles de blocs)."""
        start, size, blocks = self.pos, 0, []
        for chunk in _chunks(source, self.sb.block_size):
            _pwrite(self.fd, chunk, self.pos)
            self.pos += len(chunk)
            size += len(chunk)
            blocks.append(len(chunk) | DATA_RAW)
        return start, size, blocks

    def _flush(self, node: _Dir) -> tuple[int, int]:
        """Réécrit un dossier modifié (après ses sous-dossiers) : référence de son inode."""
        for name, child in node.children.items():
            if child.dirty:
                block, offset = self._flush(child)
                node.entries[name] = _Entry(T_DIR, child.number, block, offset)
        ordered = sorted(node.entries.items())
        block, offset, size = self._emit_listing(ordered)
        subdirs = sum(1 for _, e in ordered if e.kind == T_DIR)
        xattr = node.inode.xattr if node.inode else NO_XATTR
        parent = node.inode.parent if node.inode else (node.parent.number if node.parent else 0)
        if block >= 1 << 32 or self.it_len + len(self.new_inodes) >= 1 << 32:
            raise Unsupported("table de plus de 4 Gio")
        header = (lambda kind: struct.pack("<HHHHII", kind, node.mode, node.uid, node.gid,
                                           self.now, node.number))
        if xattr == NO_XATTR and size + 3 <= 0xFFFF:
            raw = header(T_DIR) + struct.pack("<IIHHI", block, 2 + subdirs, size + 3, offset, parent)
        else:   # étendu : i_count = 0, pas d'index (recherche linéaire)
            raw = header(T_LDIR) + struct.pack("<IIIIHHI", 2 + subdirs, size + 3, block, parent, 0,
                                               offset, xattr)
        return self._emit_inode(raw)

    # ---- changements

    def _old_file(self, entry: _Entry, path: str) -> _Inode:
        if entry.kind == T_DIR:
            raise Unsupported(f"{path} est un dossier dans l'image")
        ino = self.read_inode(entry.block, entry.offset)
        if ino.kind not in (T_REG, T_LREG, T_LNK, T_LLNK):
            raise Unsupported(f"{path} : type d'inode {ino.kind} non pris en charge")
        if ino.nlink > 1:
            raise Unsupported(f"{path} a plusieurs liens physiques")
        return ino

    def _blocks_size(self, ino: _Inode) -> int:
        return sum(b & 0xFFFFFF for b in ino.blocks)

    def _subtree_size(self, entry: _Entry) -> int:
        """Octets de données d'un fichier ou d'un dossier entier (approximation : fragments
        ignorés)."""
        total, stack = 0, [entry]
        while stack:
            cur = stack.pop()
            if cur.kind == T_DIR:
                ino = self.read_inode(cur.block, cur.offset)
                stack.extend(self.read_listing(ino).values())
            elif cur.kind in (T_REG, T_LREG):
                total += self._blocks_size(self.read_inode(cur.block, cur.offset))
        return total

    def delete(self, path: str) -> None:
        parts = [p.encode("utf-8", "surrogateescape") for p in safe_rel(path).split("/")]
        parent = self._walk(parts[:-1], create=False)
        entry = parent.entries.get(parts[-1]) if parent else None
        if parent is None or entry is None:
            return                                    # absent : rien à retirer (comme rebuild)
        if entry.kind != T_DIR:
            self._old_file(entry, path)               # refuse liens multiples et types inconnus
        self.data_dead += self._subtree_size(entry)
        parent.children.pop(parts[-1], None)
        del parent.entries[parts[-1]]
        parent.touch()

    def put(self, path: str, content: bytes | Path | None, mode: int | None = None,
            link: str | None = None) -> None:
        """Écrit un fichier (`content`) ou un lien (`link`) : remplace l'existant."""
        rel = safe_rel(path)
        parts = [p.encode("utf-8", "surrogateescape") for p in rel.split("/")]
        parent = self._walk(parts[:-1], create=True)
        assert parent is not None
        name = parts[-1]
        old_entry = parent.entries.get(name)
        old = self._old_file(old_entry, rel) if old_entry else None
        is_link = link is not None
        same_kind = old is not None and (old.kind in (T_LNK, T_LLNK)) == is_link
        if old:
            self.data_dead += self._blocks_size(old)
        number = old_entry.number if old_entry else self._number()
        uid, gid = (old.uid, old.gid) if old else (parent.uid, parent.gid)
        xattr = old.xattr if same_kind and old else NO_XATTR
        if same_kind and old:
            mode = old.mode
        elif mode is None:
            mode = 0o777 if is_link else 0o644
        if is_link:
            target = link.encode("utf-8", "surrogateescape")    # type: ignore[union-attr]
            if xattr == NO_XATTR:
                raw = struct.pack("<HHHHII", T_LNK, mode, uid, gid, self.now, number) \
                    + struct.pack("<II", 1, len(target)) + target
            else:
                raw = struct.pack("<HHHHII", T_LLNK, mode, uid, gid, self.now, number) \
                    + struct.pack("<II", 1, len(target)) + target + struct.pack("<I", xattr)
            block, offset = self._emit_inode(raw)
            kind = T_LNK
        else:
            assert content is not None
            start, size, blocks = self._write_data(content)
            raw = (struct.pack("<HHHHII", T_LREG, mode, uid, gid, self.now, number)
                   + struct.pack("<QQQIIII", start, size, 0, 1, NO_FRAGMENT, 0, xattr)
                   + struct.pack(f"<{len(blocks)}I", *blocks))
            block, offset = self._emit_inode(raw)
            kind = T_REG
        parent.entries[name] = _Entry(kind, number, block, offset)
        parent.touch()

    def apply(self, changes: Changes) -> None:
        """Mêmes changements, dans le même ordre, que `rebuild.pseudo_plan` : suppressions,
        contenus écrits, fichiers copiés, puis liens."""
        for rel in changes.delete:
            self.delete(rel)
        for rel, data in changes.write.items():
            self.put(rel, data)
        for rel, source in changes.copy.items():
            self.put(rel, source, mode=os.stat(source).st_mode & 0o7777)
        for rel, target in changes.links.items():
            self.put(rel, None, link=target)

    # ---- tables

    def _copy(self, src: int, size: int) -> None:
        done = 0
        while done < size:
            chunk = _pread(self.fd, min(1 << 20, size - done), src + done)
            _pwrite(self.fd, chunk, self.pos)
            self.pos += len(chunk)
            done += len(chunk)

    def _meta_blocks(self, buf: bytes | bytearray) -> None:
        for i in range(0, len(buf), META_SIZE):
            chunk = buf[i:i + META_SIZE]
            _pwrite(self.fd, struct.pack("<H", len(chunk) | META_RAW) + chunk, self.pos)
            self.pos += 2 + len(chunk)

    def _relocated_run(self, ptrs: list[int], index_start: int) -> int:
        """Recopie les blocs d'une table indexée puis son index décalé ; position du nouvel
        index."""
        delta = self.pos - ptrs[0]
        self._copy(ptrs[0], index_start - ptrs[0])
        new_index = self.pos
        index = struct.pack(f"<{len(ptrs)}Q", *(p + delta for p in ptrs))
        _pwrite(self.fd, index, self.pos)
        self.pos += len(index)
        return new_index

    def write_tables(self) -> _Superblock:
        """Écrit les nouvelles tables à la suite ; renvoie le nouveau superbloc."""
        sb = self.sb
        root_block, root_offset = self._flush(self.root)
        new = _Superblock(**{n: getattr(sb, n) for n in sb.__dataclass_fields__})
        new.inode_table_start = self.pos
        self._copy(sb.inode_table_start, self.it_len)
        self._meta_blocks(self.new_inodes)
        new.directory_table_start = self.pos
        self._copy(sb.directory_table_start, self.dt_len)
        self._meta_blocks(self.new_dirs)
        new.fragment_table_start = (self._relocated_run(self.frag_ptrs, sb.fragment_table_start)
                                    if self.frag_ptrs else self.pos)
        new.lookup_table_start = INVALID                 # table d'export supprimée
        new.flags &= ~FLAG_EXPORTABLE
        new.id_table_start = (self._relocated_run(self.id_ptrs, sb.id_table_start)
                              if self.id_ptrs else self.pos)
        if self.xattr_ids:
            delta = self.pos - self.xattr_kv
            self._copy(self.xattr_kv, sb.xattr_id_table_start - self.xattr_kv)
            new.xattr_id_table_start = self.pos
            header = struct.pack("<QII", self.xattr_kv + delta, self.xattr_ids, 0)
            header += struct.pack(f"<{len(self.xattr_ptrs)}Q", *(p + delta for p in self.xattr_ptrs))
            _pwrite(self.fd, header, self.pos)
            self.pos += len(header)
        new.bytes_used = self.pos
        new.root_inode = (root_block << 16) | root_offset
        new.inodes = sb.inodes + self.created
        new.mkfs_time = self.now
        return new

    def dead_estimate(self) -> int:
        """Octets rendus inutiles par cette édition : anciennes tables et données retirées."""
        return self.sb.bytes_used - self.sb.inode_table_start + self.data_dead


def _chunks(source: bytes | Path, size: int) -> Iterator[bytes]:
    """Contenu par morceaux d'un bloc de données."""
    if isinstance(source, bytes):
        for i in range(0, len(source), size):
            yield source[i:i + size]
        return
    with open(source, "rb") as f:
        while chunk := f.read(size):
            yield chunk


class _NotADir(Exception):
    pass


# ------------------------------------------------------------------ interface

def edit(image: Path, changes: Changes, in_use: Callable[[Path], bool] | None = None) -> EditResult:
    """Applique `changes` en place. `Unsupported` : rien n'a été touché, reconstruire."""
    image = Path(image)
    if changes.trees:
        raise Unsupported("arborescence entière à ajouter")
    if changes.is_empty():
        raise ManagerError("aucun changement à appliquer")
    if sb_old_path(image).exists() or image.with_name(image.name + ".old").exists():
        raise Unsupported("sauvegarde à valider ou restaurer d'abord")
    sources = list(changes.copy.values())
    try:
        new_bytes = sum(len(d) for d in changes.write.values())
        new_bytes += sum(Path(s).stat().st_size for s in sources)
        if any(not Path(s).is_file() for s in sources):
            raise Unsupported("source à copier introuvable ou pas un fichier")
    except OSError as exc:
        raise Unsupported(f"source illisible ({exc})") from exc
    if new_bytes > MAX_NEW_DATA:
        raise Unsupported(f"{new_bytes >> 20} Mio de données nouvelles (limite {MAX_NEW_DATA >> 20})")

    try:
        fd = os.open(image, os.O_RDWR)
    except OSError as exc:
        raise ManagerError(f"image illisible : {exc}") from exc
    try:
        old_size = os.fstat(fd).st_size
        raw_sb = _pread(fd, SB_SIZE, 0)
        sb = _Superblock.parse(raw_sb)
        ses = _Session(fd, sb, old_size)
        sb_old = sb_old_path(image)
        before_dead = dead_bytes(image)
        sb_written = False
        try:
            ses.apply(changes)
            if not ses.root.dirty:
                raise Unsupported("aucun changement effectif dans l'image")
            new_sb = ses.write_tables()
            end = -(-ses.pos // ALIGN) * ALIGN           # complété à un multiple de 4096
            if end > ses.pos:
                _pwrite(fd, bytes(end - ses.pos), ses.pos)
            os.fsync(fd)
            total_dead = before_dead + ses.dead_estimate()
            write_atomic(sb_old, json.dumps({
                "superblock": raw_sb.hex(), "size": old_size, "dead_bytes": before_dead,
                "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }))
            if in_use and in_use(image):
                raise ManagerError("jeu lancé pendant l'édition : image d'origine conservée")
            if _pread(fd, SB_SIZE, 0) != raw_sb:
                raise ManagerError("l'image a été modifiée pendant l'édition")
            sb_written = True
            _write_superblock(fd, new_sb.pack())
            os.fsync(fd)
        except BaseException:
            _rollback(fd, raw_sb, old_size, sb_old, sb_written)
            raise
        new_size = os.fstat(fd).st_size
    finally:
        os.close(fd)
    _set_dead(image, total_dead)
    fsync_dir(image.parent)
    return EditResult(backup=sb_old, old_size=old_size, new_size=new_size, dead_bytes=total_dead)


def _rollback(fd: int, raw_sb: bytes, old_size: int, sb_old: Path, sb_written: bool) -> None:
    """Édition interrompue : l'image retrouve son état d'origine (superbloc, taille)."""
    try:
        if sb_written:
            _pwrite(fd, raw_sb, 0)
        os.ftruncate(fd, old_size)
        os.fsync(fd)
    finally:
        sb_old.unlink(missing_ok=True)


def _read_backup(image: Path) -> dict:
    sb_old = sb_old_path(image)
    try:
        info = json.loads(sb_old.read_text(encoding="utf-8"))
        raw = bytes.fromhex(info["superblock"])
        int(info["size"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if not sb_old.exists():
            raise ManagerError("aucune sauvegarde .sb-old") from exc
        raise ManagerError(f"sauvegarde .sb-old illisible : {exc}") from exc
    if len(raw) != SB_SIZE or _Superblock.parse(raw).magic != MAGIC:
        raise ManagerError("sauvegarde .sb-old invalide (superbloc)")
    return info


def undo(image: Path) -> None:
    """Restaure la version d'avant la dernière édition : superbloc d'origine remis, fichier
    retronqué à sa taille d'origine, sauvegarde supprimée."""
    image = Path(image)
    info = _read_backup(image)
    old_size = int(info["size"])
    try:
        with open(image, "r+b", buffering=0) as f:
            fd = f.fileno()
            if os.fstat(fd).st_size < old_size:
                raise ManagerError("image plus courte que l'original : restauration impossible")
            _pwrite(fd, bytes.fromhex(info["superblock"]), 0)
            os.ftruncate(fd, old_size)
            os.fsync(fd)
    except OSError as exc:
        raise ManagerError(f"restauration impossible : {exc}") from exc
    _set_dead(image, int(info.get("dead_bytes", 0)))
    sb_old_path(image).unlink()
    fsync_dir(image.parent)


def undo_possible(image: Path) -> bool:
    """La sauvegarde du superbloc est lisible et le fichier au moins aussi long qu'à l'origine."""
    try:
        return Path(image).stat().st_size >= int(_read_backup(Path(image))["size"])
    except (OSError, ManagerError):
        return False


def commit(image: Path) -> None:
    """Valide l'édition : la sauvegarde du superbloc est supprimée."""
    image = Path(image)
    sb_old = sb_old_path(image)
    if not sb_old.exists():
        raise ManagerError("aucune sauvegarde .sb-old")
    sb_old.unlink()
    fsync_dir(image.parent)
