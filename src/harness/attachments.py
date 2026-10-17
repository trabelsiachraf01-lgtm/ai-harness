"""Images in a user message: `@path/to/shot.png` attaches the file for that turn. A path dropped
into the terminal (absolute, `~/` or `./`, no `@`) attaches too, if the file exists.

Only references that end in an image extension count, so `@mentions` stay plain text. A missing or
oversized `@image` is an error, never silently dropped: the user would think the model saw it.
"""

import base64
import re
from dataclasses import dataclass
from pathlib import Path

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
               ".gif": "image/gif"}  # what OpenRouter accepts
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # base64 adds a third on top
_REF = re.compile(r"""(?<!\S)(@?)("[^"]+"|'[^']+'|(?:\\.|\S)+)""")  # quoted, or backslash-escaped spaces (drag-and-drop)


class AttachmentError(Exception):
    pass


@dataclass(frozen=True)
class Image:
    """A reference to a file on disk. Bytes are read when needed, so the session stores only the path."""
    name: str
    path: Path

    @property
    def size(self) -> int:
        return self.path.stat().st_size

    @property
    def data_url(self) -> str:
        encoded = base64.b64encode(self.path.read_bytes()).decode()
        return f"data:{IMAGE_TYPES[self.path.suffix.lower()]};base64,{encoded}"


def find_images(message: str, base: Path) -> list[Image]:
    """Every distinct `@image` reference in the message, read and encoded. Relative paths resolve from `base`."""
    images: dict[Path, Image] = {}
    for match in _REF.finditer(message):
        explicit, raw = match.group(1) == "@", match.group(2)
        raw = raw[1:-1] if raw[0] in "\"'" else re.sub(r"\\(.)", r"\1", raw).rstrip(".,;:!?)")
        path = base / Path(raw).expanduser()
        if path.suffix.lower() not in IMAGE_TYPES or path in images:
            continue
        if explicit or (raw.startswith(("/", "~/", "./")) and path.is_file()):
            images[path] = _load(path)
    return list(images.values())


def _load(path: Path) -> Image:
    if not path.is_file():
        raise AttachmentError(f"image not found: {path}")
    size = path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise AttachmentError(f"{path.name} is {size / 1e6:.1f} MB, the limit is {MAX_IMAGE_BYTES // 2**20} MB")
    return Image(path.name, path)
