"""
Largeur / hauteur d'affichage des photos de post (`PostPhoto.width` / `height`).

L'app cadre la photo d'un post au ratio de la 1re photo (borné 4:5 → 1,91:1) au lieu d'un carré
forcé ; elle a besoin des dimensions **dans le payload** (aucun appel réseau par photo côté app).

- Upload multipart (`request.FILES`) : lecture directe du fichier (`dimensions_from_file`).
- Upload direct S3 (URL présignée puis `confirm_photo_upload`, `/posts/{id}/photos/`, retouche) : le back
  ne voit pas l'image → tâche Celery après commit (`schedule_photo_dimensions`) qui lit seulement l'en-tête.
- Photos existantes : `python manage.py backfill_post_photo_dimensions`.

Dimensions **d'affichage** : une orientation EXIF 5-8 (photo tournée de 90°) inverse largeur et hauteur.
Toute erreur (fichier absent, illisible, réseau) → None : la photo reste sans taille, l'app garde le carré.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from PIL import Image, ImageFile

logger = logging.getLogger(__name__)

# Un en-tête JPEG (EXIF + vignette compris) tient presque toujours dans les premiers 64 Ko ; on s'arrête
# dès que la taille est connue, et jamais au-delà de MAX_READ_BYTES.
CHUNK_BYTES = 16 * 1024
MAX_READ_BYTES = 8 * 1024 * 1024
FETCH_TIMEOUT = (5, 15)
EXIF_ORIENTATION_TAG = 0x0112
SWAPPED_ORIENTATIONS = {5, 6, 7, 8}


def _display_size(image: Image.Image) -> tuple[int, int] | None:
    width, height = image.size
    if not width or not height or width <= 0 or height <= 0:
        return None
    try:
        orientation = image.getexif().get(EXIF_ORIENTATION_TAG)
    except Exception:
        orientation = None
    if orientation in SWAPPED_ORIENTATIONS:
        return height, width
    return width, height


def dimensions_from_chunks(chunks: Iterable[bytes], max_bytes: int = MAX_READ_BYTES) -> tuple[int, int] | None:
    """Taille d'affichage en lisant le flux jusqu'à ce que l'en-tête soit décodé (None si illisible)."""
    parser = ImageFile.Parser()
    read = 0
    try:
        for chunk in chunks:
            if not chunk:
                continue
            read += len(chunk)
            parser.feed(chunk)
            if parser.image is not None:
                return _display_size(parser.image)
            if read >= max_bytes:
                break
    except Exception:
        logger.debug('[photo_dimensions] en-tête illisible', exc_info=True)
        return None
    return _display_size(parser.image) if parser.image is not None else None


def dimensions_from_file(fileobj) -> tuple[int, int] | None:
    """Taille d'affichage d'un fichier uploadé (le curseur est remis au début)."""
    try:
        if hasattr(fileobj, 'seek'):
            fileobj.seek(0)
        with Image.open(fileobj) as image:
            return _display_size(image)
    except Exception:
        logger.debug('[photo_dimensions] fichier illisible', exc_info=True)
        return None
    finally:
        try:
            if hasattr(fileobj, 'seek'):
                fileobj.seek(0)
        except Exception:
            pass


def _photo_fetch_url(image_path: str) -> str | None:
    path = (image_path or '').strip()
    if not path:
        return None
    if path.startswith('http'):
        return path
    from savr_back.settings import build_presigned_get_url

    return build_presigned_get_url(path.replace('s3:/', '').lstrip('/'))


def fetch_image_dimensions(image_path: str) -> tuple[int, int] | None:
    """Taille d'affichage d'une image du bucket (ou d'une URL absolue), en ne lisant que l'en-tête."""
    url = _photo_fetch_url(image_path)
    if not url:
        return None
    try:
        if image_path.strip().startswith('http'):
            # URL absolue stockée telle quelle (legacy) : même garde-fou SSRF que les imports.
            from core.url_safety import safe_get

            response = safe_get(url, stream=True, timeout=FETCH_TIMEOUT)
        else:
            import requests

            response = requests.get(url, stream=True, timeout=FETCH_TIMEOUT)
    except Exception as exc:
        logger.info('[photo_dimensions] %s injoignable : %s', image_path, exc)
        return None
    try:
        if response.status_code != 200:
            logger.info('[photo_dimensions] %s : HTTP %s', image_path, response.status_code)
            return None
        return dimensions_from_chunks(response.iter_content(CHUNK_BYTES))
    except Exception as exc:
        logger.info('[photo_dimensions] %s : lecture impossible (%s)', image_path, exc)
        return None
    finally:
        response.close()


def fill_photo_dimensions(photo, *, force: bool = False) -> bool:
    """Lit et enregistre la taille d'une photo. True si une taille a été écrite."""
    if not force and photo.width and photo.height:
        return False
    size = fetch_image_dimensions(photo.image_path)
    if not size:
        return False
    photo.width, photo.height = size
    type(photo).objects.filter(pk=photo.pk).update(width=photo.width, height=photo.height)
    return True


def schedule_photo_dimensions(photos) -> None:
    """Planifie (après commit) la lecture de la taille des photos qui n'en ont pas encore."""
    ids = [p.id for p in photos if p is not None and p.id and p.image_path and not (p.width and p.height)]
    if not ids:
        return
    from core.celery_dispatch import enqueue_on_commit

    from ..tasks import fill_post_photo_dimensions

    enqueue_on_commit(fill_post_photo_dimensions, args=[ids])
