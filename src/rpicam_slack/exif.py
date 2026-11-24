"""Optional EXIF note (capture settings) written with ExifTool. Never fatal."""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from .config import OutputConfig

log = logging.getLogger(__name__)


def make_embedder(cfg: OutputConfig) -> Callable[[Path, str], None] | None:
    """A function ``embed(image, summary)``, or ``None`` when EXIF embedding is off/unavailable."""
    if not cfg.embed_exif:
        return None
    if shutil.which(cfg.exiftool) is None:
        log.warning("%s not found; EXIF notes are skipped", cfg.exiftool)
        return None

    def embed(image: Path, summary: str) -> None:
        args = [
            cfg.exiftool,
            "-overwrite_original",
            "-q",
            "-EXIF:Software=rpicam-slack",
            f"-EXIF:UserComment={summary}",
            str(image),
        ]
        try:
            subprocess.run(args, check=True, capture_output=True, timeout=cfg.exif_timeout_s)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("EXIF embedding failed (the image is still posted): %s", exc)

    return embed
