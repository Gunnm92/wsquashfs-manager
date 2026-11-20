"""Lecture des exécutables PE (sans les exécuter) : bits, sous-système, imports, ressources de
version et marqueurs cherchés dans les octets."""

from __future__ import annotations

import mmap
from dataclasses import dataclass, field
from pathlib import Path

import pefile

# Marqueurs cherchés dans l'exécutable (règle « strings_any »)
_MARKERS = (b"libScePad", b"DualSense", b"DUALSENSE")
_MARKER_SCAN_LIMIT = 400 * 2**20


@dataclass
class ExeInfo:
    bits: int | None = None
    gui: bool | None = None
    large_address_aware: bool = False
    imports: set[str] = field(default_factory=set)
    product_name: str | None = None
    product_version: str | None = None
    file_version: str | None = None
    markers: set[str] = field(default_factory=set)
    error: str | None = None


def _version_string(value: bytes | str | None) -> str | None:
    if not value:
        return None
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    text = text.strip().strip("\0").replace(", ", ".").replace(",", ".")
    return text or None


def read_exe(path: Path) -> ExeInfo:
    """En-têtes, imports et version d'un exécutable PE (sans l'exécuter)."""
    info = ExeInfo()
    try:
        pe = pefile.PE(str(path), fast_load=True)
    except (pefile.PEFormatError, OSError) as exc:
        info.error = str(exc)
        return info
    try:
        machine = pe.FILE_HEADER.Machine
        info.bits = 64 if machine in (0x8664, 0xAA64) else 32 if machine == 0x14C else None
        info.gui = pe.OPTIONAL_HEADER.Subsystem == 2
        info.large_address_aware = bool(pe.FILE_HEADER.Characteristics & 0x20)
        pe.parse_data_directories(directories=[
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"],
        ])
        for attr in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
            for entry in getattr(pe, attr, []) or []:
                if entry.dll:
                    info.imports.add(entry.dll.decode("latin-1").lower())
        for file_info in getattr(pe, "FileInfo", []) or []:
            for block in file_info:
                for table in getattr(block, "StringTable", []) or []:
                    strings = {k.decode("latin-1"): v for k, v in table.entries.items()}
                    info.product_name = info.product_name or _version_string(
                        strings.get("ProductName"))
                    info.product_version = info.product_version or _version_string(
                        strings.get("ProductVersion"))
                    info.file_version = info.file_version or _version_string(
                        strings.get("FileVersion"))
    except Exception as exc:  # noqa: BLE001 — un PE malformé ne bloque pas l'analyse
        info.error = str(exc)
    finally:
        pe.close()
    info.markers = _markers(path)
    return info


def _markers(path: Path) -> set[str]:
    try:
        if path.stat().st_size > _MARKER_SCAN_LIMIT:
            return set()
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            return {marker.decode() for marker in _MARKERS if m.find(marker) != -1}
    except (OSError, ValueError):
        return set()
