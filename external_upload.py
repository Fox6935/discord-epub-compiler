import io
from typing import Any, Dict

import aiohttp

from config import EXTERNAL_UPLOAD_KEY, EXTERNAL_UPLOAD_URL, HTTP_TIMEOUT_SECONDS


def external_upload_enabled() -> bool:
    return bool(EXTERNAL_UPLOAD_URL and EXTERNAL_UPLOAD_KEY)


async def upload_epub_bytes(
    output_bytes: bytes,
    filename: str,
) -> str:
    if not external_upload_enabled():
        raise RuntimeError("External upload is not configured")

    form = aiohttp.FormData()
    form.add_field("allowedDownloads", "10")
    form.add_field("expiryDays", "5")
    form.add_field(
        "file",
        io.BytesIO(output_bytes),
        filename=filename,
        content_type="application/octet-stream",
    )

    headers = {
        "accept": "application/json",
        "apikey": EXTERNAL_UPLOAD_KEY,
    }
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(EXTERNAL_UPLOAD_URL, headers=headers, data=form) as response:
            response.raise_for_status()
            payload: Dict[str, Any] = await response.json(content_type=None)

    base_url = str(payload.get("Url") or "")
    file_info = payload.get("FileInfo") or {}
    file_id = str(file_info.get("Id") or "")

    if not base_url or not file_id:
        raise RuntimeError("External upload response did not include a download link")

    return base_url + file_id
