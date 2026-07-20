"""Image utilities — base64 detection, saving, local file detection."""

import base64
import os
import tempfile

from ..config import CACHE_DIR


def is_base64_image(data: str) -> bool:
    """Check if the input looks like base64-encoded image data."""
    if data.startswith("data:image/"):
        return True
    if len(data) > 200 and "/" not in data[:50] and not data.startswith(("http", "~")):
        try:
            base64.b64decode(data[:100] + "==", validate=True)
            return True
        except Exception:
            pass
    return False


def save_base64_image(data: str) -> str:
    """Save base64 image data to a temp file and return the path."""
    if data.startswith("data:image/"):
        header, b64data = data.split(",", 1)
        mime = header.split(";")[0].split(":")[1]
        ext = mime.split("/")[1].replace("jpeg", "jpg")
    else:
        b64data = data
        ext = "png"

    img_bytes = base64.b64decode(b64data)
    os.makedirs(CACHE_DIR, exist_ok=True)

    tmp = tempfile.NamedTemporaryFile(
        suffix=f".{ext}", prefix="mcp_img_", delete=False, dir=CACHE_DIR,
    )
    tmp.write(img_bytes)
    tmp.close()
    return tmp.name


def is_local_file(path: str) -> bool:
    """Check if the input looks like a local file path rather than a URL."""
    if path.startswith(("http://", "https://", "data:")):
        return False
    return path.startswith(("/", "~", "./", "../")) or os.path.exists(path)
