# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Resolve static showcase images (e.g. method overview) under ``data/``."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# Filename stem (no extension), as stored under data/ (any subfolder).
_FOCUS_DIAGRAM_STEM = (
    "FOCUS Unified Vision-Language Modeling for Interactive Editing Driven by "
    "Referential Segmentation_diagram"
)

_RASTER_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


def find_focus_system_overview_diagram(project_root: Path) -> Optional[Path]:
    """
    Return the first matching raster under ``<project_root>/data/`` whose stem
    equals the FOCUS paper diagram name, or ``None``.
    """
    data = (project_root / "data").resolve()
    if not data.is_dir():
        return None

    for suf in _RASTER_SUFFIXES:
        direct = data / f"{_FOCUS_DIAGRAM_STEM}{suf}"
        if direct.is_file():
            return direct

    try:
        for p in data.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in _RASTER_SUFFIXES:
                continue
            if p.stem == _FOCUS_DIAGRAM_STEM:
                return p
    except OSError:
        return None
    return None
