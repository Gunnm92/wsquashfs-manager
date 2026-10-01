"""Lecture des en-têtes PE et de la table d'imports (SPEC § 2.5).

Lecture seule, sans Wine : bits (32/64), sous-système, drapeau « large
address aware », liste des DLL importées, et ressources de version
(ProductVersion / FileVersion / ProductName / FileDescription).

Tolérance à une structure incomplète : on lit ce qui est lisible et on s'arrête
plutôt que de lever une erreur (un fichier n'est pas forcément un PE valide).
Les chaînes des ressources de version sont lues jusqu'au \0 (UTF-16) et les
blocs enfants sont parcourus via leur longueur (`wLength`) : on ne fait pas
confiance à `wValueLength`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

RT_VERSION = 24   # type de ressource « VERSION »


@dataclass
class ExeInfo:
    """Ce qu'on retient d'un exécutable pour la génération d'autorun."""
    path: str = ""                 # chemin relatif à la racine (display)
    exists: bool = False
    is_pe: bool = False
    bits: int | None = None        # 32 | 64
    subsystem: str = ""            # "gui" | "console" | ""
    large_address_aware: bool = False
    imports: set[str] = field(default_factory=set)   # noms de DLL (sans .dll, minuscules)
    size: int = 0
    product_version: str | None = None
    file_version: str | None = None
    product_name: str | None = None
    file_description: str | None = None
    error: str | None = None       # pourquoi ce n'est pas un PE lisible


class _Truncated(Exception):
    pass


def _u16(buf, off):
    if off + 2 > len(buf):
        raise _Truncated()
    return struct.unpack_from("<H", buf, off)[0]


def _u32(buf, off):
    if off + 4 > len(buf):
        raise _Truncated()
    return struct.unpack_from("<I", buf, off)[0]


def _cstr(buf, off):
    """Chaîne ANSI terminée par \0 (bornée à 256)."""
    end = buf.find(b"\0", off)
    return buf[off:min(len(buf), off + 256) if end < 0 else end].decode("latin-1")


def _ustr_null(buf, off, limit):
    """Chaîne UTF-16 terminée par \0, bornée à `limit` (octets)."""
    end = len(buf)
    if limit < end:
        end = limit
    # \0\0 = deux octets nuls consécutifs
    i = off
    out = []
    while i + 1 < end:
        if buf[i] == 0 and buf[i + 1] == 0:
            break
        out.append(buf[i])
        i += 1
    return b"".join(out).decode("utf-16-le", errors="replace")


# ------------------------------------------------------------- version resource

def _parse_version_node(buf, off):
    """Un nœud VS_VERSIONINFO : `wLength` (2), puis clé et valeur UTF-16.

    `wType == 0` : nœud texte. Si `wValueLength == 0` c'est un conteneur
    (`VS_VERSION_INFO`, `StringFileInfo`, bloc par locale) dont les enfants
    suivent ; sinon c'est une paire clé/valeur (`ProductName`…). `wType != 0` :
    binaire (`VS_FIXEDFILEINFO`) — ignoré. Totalement tolérant : toute zone
    incohérente s'arrête là."""
    if off + 16 > len(buf):
        return {}
    wType, wLength, wValueLength = struct.unpack_from("<HHH", buf, off)
    if wLength < 16 or off + wLength > len(buf):
        return {}
    node_end = off + wLength
    key = _ustr_null(buf, off + 8, node_end)
    if wType != 0:
        return {}                                  # VS_FIXEDFILEINFO et al.
    if wValueLength:                               # nœud texte (clé/valeur)
        value = _ustr_null(buf, off + 16, node_end)
        return {key: value} if key else {}
    # Conteneur : on parcourt les enfants via leurs longueurs.
    out: dict[str, str] = {}
    child = off + 16
    while child < node_end:
        if child + 4 > len(buf):
            break
        ctype, clen = struct.unpack_from("<HH", buf, child)
        if clen <= 16 or child + clen > len(buf):
            break
        out.update(_parse_version_node(buf, child))
        child += clen
        if child % 4:
            child += 4 - (child % 4)
    return out


def _walk_resources(buf, off, size):
    """Retourne la ressource de version (type `RT_VERSION`), si présente.

    `off` pointe sur l'IMAGE_RESOURCE_DIRECTORY racine ; `size` borne la zone.
    Trois niveaux (type → id → langue) ; chaque langue pointe vers le bloc
    VS_VERSIONINFO. On ne lit que le type 24. Tout écart de structure → {}."""
    end = min(off + size, len(buf))
    if off + 16 > end:
        return {}
    _chars, _ts, _maj, _min, n_named, n_ids = struct.unpack_from("<IHIHHH", buf, off)
    entries = off + 16
    for i in range(n_named + n_ids):
        e = entries + i * 8
        if e + 8 > end:
            break
        rid, sub = struct.unpack_from("<II", buf, e)
        if rid >> 31 or sub & 0x80000000:
            continue                      # entry nommée ou sous-répertoire inattendu
        if rid != RT_VERSION:
            continue
        sub &= 0x7FFFFFFF
        if off + sub + 16 > end:
            break
        d2 = off + sub
        _c2, _t2, _m2, _n2, nn2, ni2 = struct.unpack_from("<IHIHHH", buf, d2)
        e2 = d2 + 16
        for j in range(nn2 + ni2):
            ej = e2 + j * 8
            if ej + 8 > end:
                break
            _rid2, dl = struct.unpack_from("<II", buf, ej)
            if dl & 0x80000000:
                continue
            dl &= 0x7FFFFFFF
            if off + dl + 16 > end:
                continue
            d3 = off + dl
            _c3, _t3, _m3, _n3, nn3, ni3 = struct.unpack_from("<IHIHHH", buf, d3)
            e3 = d3 + 16
            for k in range(nn3 + ni3):
                ek = e3 + k * 16
                if ek + 16 > end:
                    break
                data_off, data_size = struct.unpack_from("<II", buf, ek)
                data_abs = off + (data_off & 0x7FFFFFFF)
                if data_off >> 31 or data_size == 0 or data_abs + data_size > len(buf):
                    continue
                found = _parse_version_node(buf, data_abs)
                if found:
                    return found
    return {}


# ------------------------------------------------------------------- PE header

def _parse_imports(buf, import_rva, rva_to_off, image_base):
    """Table d'imports → noms de DLL (minuscules, sans `.dll`)."""
    dlls: set[str] = set()
    if import_rva == 0:
        return dlls
    off = rva_to_off(import_rva)
    if off is None:
        return dlls
    for i in range(64):                        # IMAGE_IMPORT_DESCRIPTOR, zéro = fin
        if off + 20 * (i + 1) > len(buf):
            break
        ilt, _ts, _fwd, name_rva, iat = struct.unpack_from("<IIIII", buf, off + i * 20)
        if ilt == 0 and name_rva == 0 and iat == 0:
            break
        if name_rva:
            noff = rva_to_off(name_rva)
            if noff is not None:
                dlls.add(_cstr(buf, noff).lower())
    return dlls


def parse_pe(buf: bytes, path: str = "") -> ExeInfo:
    """Analyse les octets d'un exécutable. Ne lève pas sur PE invalide :
    `is_pe` vaut alors False et `error` explique pourquoi."""
    info = ExeInfo(path=path, exists=True, size=len(buf))
    if len(buf) < 2:
        info.error = "fichier trop petit"
        return info
    if buf[0:2] != b"MZ":
        info.error = "pas de signature MZ"
        return info

    try:
        e_lfanew = _u32(buf, 0x3C)
        if buf[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
            info.error = "pas de signature PE"
            return info
        info.is_pe = True
        coff = e_lfanew + 4
        machine, num_sections, _ts, _ptr, num_syms, size_opt, characteristics = \
            struct.unpack_from("<HHIIIHH", buf, coff)
        if not (0 < num_sections < 64) or num_syms > 10_000_000:
            info.error = "sections incohérentes"
            return info

        opt = coff + 20
        magic, = struct.unpack_from("<H", buf, opt)
        pe32plus = (magic == 0x20B)
        if magic not in (0x10B, 0x20B):
            info.error = "magic optionnel inconnu"
            return info

        info.bits = 64 if pe32plus else 32
        info.large_address_aware = bool(characteristics & 0x0020)
        if pe32plus:
            if opt + 24 + 8 > len(buf):
                raise _Truncated()
            image_base = struct.unpack_from("<Q", buf, opt + 24)[0]
        else:
            image_base = _u32(buf, opt + 28)

        info.subsystem = {2: "gui", 3: "console"}.get(_u16(buf, opt + 68), "")

        # Sections : conversion RVA → offset physique.
        sec = opt + size_opt
        sections = []
        for i in range(num_sections):
            o = sec + i * 40
            if o + 40 > len(buf):
                break
            vsize, va_rva, rawsize, rawptr = struct.unpack_from("<IIII", buf, o + 8)
            sections.append((va_rva, max(vsize, rawsize), rawptr))

        def rva_to_off(rva):
            for va, span, rawptr in sections:
                if va <= rva < va + span:
                    return rawptr + (rva - va)
            return None

        dd = opt + (96 if pe32plus else 92)
        if dd + 8 > len(buf):
            info.error = "data directories hors limites"
            return info
        import_rva, _isize = struct.unpack_from("<II", buf, dd)
        info.imports = _parse_imports(buf, import_rva, rva_to_off, image_base)

        res_rva, res_size = struct.unpack_from("<II", buf, dd + 16)
        if res_rva:
            roff = rva_to_off(res_rva)
            if roff is not None:
                v = _walk_resources(buf, roff, res_size)
                info.product_version = v.get("ProductVersion")
                info.file_version = v.get("FileVersion")
                info.product_name = v.get("ProductName")
                info.file_description = v.get("FileDescription")
    except (_Truncated, struct.error, IndexError):
        if not info.is_pe:
            info.error = "en-tête PE illisible"
    return info
