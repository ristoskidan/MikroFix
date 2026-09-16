"""Varno shranjevanje in odpiranje servisnih fotografij."""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_SERVICE_IMAGE_BYTES = 25 * 1024 * 1024
MAX_SERVICE_IMAGE_PIXELS = 80_000_000
ALLOWED_SERVICE_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
ALLOWED_SERVICE_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "BMP"}


class ServiceImageError(ValueError):
    """Fotografija ni primerna za varno hrambo ali prikaz."""


def _validated_source(path: Path) -> Path:
    source = Path(path)
    if not source.is_file():
        raise ServiceImageError("Izbrana fotografija ne obstaja.")
    if source.suffix.lower() not in ALLOWED_SERVICE_IMAGE_SUFFIXES:
        raise ServiceImageError("Dovoljene so samo fotografije JPG, PNG, WEBP ali BMP.")
    if source.stat().st_size > MAX_SERVICE_IMAGE_BYTES:
        raise ServiceImageError("Fotografija je večja od dovoljenih 25 MB.")
    return source


def load_service_image(path: Path) -> Image.Image:
    """Preveri vsebino in vrne ločeno, dekodirano sliko brez odprte datoteke."""
    source = _validated_source(path)
    try:
        with Image.open(source) as opened:
            if str(opened.format or "").upper() not in ALLOWED_SERVICE_IMAGE_FORMATS:
                raise ServiceImageError("Vsebina datoteke ni podprta fotografija.")
            width, height = opened.size
            if width <= 0 or height <= 0 or width * height > MAX_SERVICE_IMAGE_PIXELS:
                raise ServiceImageError("Ločljivost fotografije presega varnostno omejitev.")
            if getattr(opened, "n_frames", 1) > 1:
                opened.seek(0)
            return ImageOps.exif_transpose(opened).convert("RGB").copy()
    except ServiceImageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ServiceImageError("Datoteke ni mogoče potrditi kot veljavno fotografijo.") from exc


def archive_service_image(source: Path, target_dir: Path, base_name: str) -> Path:
    """Fotografijo ponovno kodira v varen JPG in jo atomsko shrani."""
    image = load_service_image(source)
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    destination = target_dir / f"{base_name}.jpg"
    counter = 1
    while destination.exists():
        destination = target_dir / f"{base_name}_{counter}.jpg"
        counter += 1

    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        image.save(temporary, format="JPEG", quality=94, optimize=True)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise
    finally:
        image.close()
    return destination


def resolve_service_image_path(
    stored_path: str,
    *,
    data_dir: Path,
    app_dir: Path | None = None,
) -> Path:
    """Razreši novo ali staro pot, vendar samo znotraj dovoljenih podatkovnih map."""
    raw = Path(str(stored_path or "").strip())
    roots = [Path(data_dir).resolve()]
    if app_dir is not None:
        roots.append(Path(app_dir).resolve())

    candidates = [raw] if raw.is_absolute() else [root / raw for root in roots]
    for candidate in candidates:
        resolved = candidate.resolve()
        matching_root = next(
            (root for root in roots if resolved == root or root in resolved.parents),
            None,
        )
        if matching_root is None:
            continue
        relative_parts = [part.lower() for part in resolved.relative_to(matching_root).parts]
        is_service_photo = any(
            relative_parts[index:index + 2] == ["service", "photos"]
            for index in range(max(0, len(relative_parts) - 1))
        )
        if is_service_photo and resolved.is_file():
            return resolved
    raise ServiceImageError("Fotografija ne obstaja v dovoljeni podatkovni mapi.")
