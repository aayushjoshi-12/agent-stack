"""Images in messages (P44): one block shape, content-addressed names, text placeholders.

An image travels as an OpenAI-style ``image_url`` block holding a base64 data URL. It is the one
shape every provider path here accepts in both user and tool messages (ChatOpenRouter converts
LangChain's v1 ``image`` blocks for user messages only and sends tool content as it is).
Everywhere a person or a log reads a message, an image is a short ``[image: <name>]`` instead.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Anthropic's per-image limit; a bigger image is refused, not shrunk (no imaging dependency).
MAX_IMAGE_BYTES = 5 * 1024 * 1024
#: What one image costs in the token estimate (a ~1100x1100 image on Anthropic's 750 px/token).
IMAGE_TOKENS = 1600
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")


@dataclass(frozen=True)
class Image:
    """An image attached to a message: its bytes, type and the name a person knows it by."""

    data: bytes
    mime: str
    name: str = "image"


def sniff_mime(data: bytes) -> str | None:
    """The image type from the file's first bytes (never trusted from a name or a header)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def make_image(data: bytes, name: str = "image") -> Image:
    """Check ``data`` is a PNG, JPEG, WebP or GIF within the size cap; ValueError otherwise."""
    _check_size(len(data), name)
    mime = sniff_mime(data)
    if mime is None:
        raise ValueError(f"{name} is not a PNG, JPEG, WebP or GIF image")
    return Image(data=data, mime=mime, name=name)


def load_image(path: Path) -> Image:
    """Read an image file from disk (size checked before it is read)."""
    _check_size(path.stat().st_size, path.name)
    return make_image(path.read_bytes(), path.name)


def _check_size(size: int, name: str) -> None:
    if size > MAX_IMAGE_BYTES:
        raise ValueError(
            f"{name} is {size / 1_048_576:.1f} MB; images are limited to "
            f"{MAX_IMAGE_BYTES // 1_048_576} MB"
        )


def image_block(data: bytes, mime: str) -> dict[str, Any]:
    """The content block a model receives for one image."""
    url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    return {"type": "image_url", "image_url": {"url": url}}


def decode_image(block: object) -> tuple[bytes, str] | None:
    """``(bytes, mime)`` of an image block with inline data; None for anything else."""
    if not isinstance(block, Mapping) or block.get("type") != "image_url":
        return None
    image_url = block.get("image_url")
    url = image_url.get("url") if isinstance(image_url, Mapping) else image_url
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    header, _, payload = url[5:].partition(",")
    mime, _, encoding = header.partition(";")
    if encoding != "base64" or mime not in EXTENSIONS:
        return None
    try:
        return base64.b64decode(payload, validate=True), mime
    except ValueError:
        return None


def image_file_name(data: bytes, mime: str) -> str:
    """The content-addressed file name an image is stored under: ``<sha256[:16]>.<ext>``."""
    return f"{hashlib.sha256(data).hexdigest()[:16]}.{EXTENSIONS.get(mime, 'bin')}"


def block_name(block: object) -> str | None:
    """The stored file name of an image block (None when ``block`` is not one)."""
    decoded = decode_image(block)
    return image_file_name(*decoded) if decoded else None


def placeholder(name: str) -> str:
    return f"[image: {name}]"


def is_image_block(block: object) -> bool:
    return isinstance(block, Mapping) and block.get("type") == "image_url"


def content_text(content: object) -> str:
    """The text of a message's content, images left out (a string passes through)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, Mapping) and isinstance(part.get("text"), str):
            parts.append(str(part["text"]))
    return "".join(parts)


def image_count(content: object) -> int:
    return sum(1 for part in content if is_image_block(part)) if isinstance(content, list) else 0


def content_chars(content: object) -> int:
    """Characters for the token estimate: the text, plus a fixed cost per image (not base64)."""
    if not isinstance(content, list):
        return len(str(content))
    return len(content_text(content)) + 4 * IMAGE_TOKENS * image_count(content)


def without_images(content: object, note: str = "") -> object:
    """``content`` with each image replaced by its placeholder (plus ``note``) as text."""
    if not image_count(content):
        return content
    assert isinstance(content, list)
    return [
        {"type": "text", "text": f"{placeholder(block_name(part) or 'image')}{note}"}
        if is_image_block(part)
        else part
        for part in content
    ]
