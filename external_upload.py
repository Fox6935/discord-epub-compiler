import io
import json
from typing import Any, Dict

import aiohttp

from config import EXTERNAL_UPLOAD_KEY, EXTERNAL_UPLOAD_URL, HTTP_TIMEOUT_SECONDS


def external_upload_enabled() -> bool:
    return bool(EXTERNAL_UPLOAD_URL and EXTERNAL_UPLOAD_KEY)


def download_url_from_response(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise RuntimeError("External upload returned an invalid JSON response")

    if payload.get("Result") != "OK":
        detail = str(payload.get("ErrorMessage") or "unknown API error")
        raise RuntimeError(f"External upload was rejected: {detail}")

    file_info = payload.get("FileInfo")
    if not isinstance(file_info, dict):
        raise RuntimeError("External upload response did not include FileInfo")

    base_url = payload.get("Url")
    file_id = file_info.get("Id")
    if isinstance(base_url, str) and base_url and isinstance(file_id, str) and file_id:
        return base_url + file_id

    download_url = file_info.get("UrlDownload")
    if not isinstance(download_url, str) or not download_url:
        raise RuntimeError(
            "External upload response did not include Url + FileInfo.Id "
            "or FileInfo.UrlDownload"
        )

    return download_url


async def upload_epub_bytes(
    output_bytes: bytes,
    filename: str,
) -> str:
    if not external_upload_enabled():
        raise RuntimeError("External upload is not configured")

    form = aiohttp.FormData()
    form.add_field("allowedDownloads", "10")
    form.add_field("expiryDays", "1")
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
            response_text = await response.text()

            try:
                payload: Dict[str, Any] = json.loads(response_text)
            except (TypeError, json.JSONDecodeError) as exc:
                if response.status >= 400:
                    detail = response_text.strip()[:500] or response.reason
                    raise RuntimeError(
                        f"External upload failed with HTTP {response.status}: {detail}"
                    ) from exc
                raise RuntimeError("External upload returned invalid JSON") from exc

            if response.status >= 400:
                api_error = payload.get("ErrorMessage") if isinstance(payload, dict) else None
                detail = str(api_error or response.reason)
                raise RuntimeError(
                    f"External upload failed with HTTP {response.status}: {detail}"
                )

    return download_url_from_response(payload)
