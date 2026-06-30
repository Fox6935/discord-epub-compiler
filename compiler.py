import asyncio
import traceback
import zipfile
from typing import Dict, List, Optional, Set, Tuple

from config import IMAGE_SIZE_ABORT_RATIO, MAX_OUTPUT_EPUB_BYTES
from db import ARCHIVE
from epub_tools import (
    build_compiled_epub_bytes, estimate_compiled_epub_bytes, extract_book_content,
    format_bytes,
)
from models import EpubEntry, OutputTooLargeError, DEBUG_LOGS, log_warning


async def compile_selected_epubs(
    selected: List[EpubEntry],
    title: str,
    author: str,
    remove_all_images: bool = False,
    max_output_bytes: int = MAX_OUTPUT_EPUB_BYTES,
) -> Tuple[Optional[bytes], List[Tuple[str, str]]]:
    used_chapter_names: Set[str] = set()
    used_image_names: Set[str] = set()
    image_hash_to_name: Dict[str, str] = {}

    final_chapters: List[Tuple[str, str, bytes]] = []
    final_images: Dict[str, bytes] = {}
    skipped: List[Tuple[str, str]] = []

    for entry_index, entry in enumerate(selected, start=1):
        try:
            epub_bytes = await ARCHIVE.reconstruct_epub(entry.epub_version_id)
            chapters, images = await asyncio.to_thread(
                extract_book_content,
                epub_bytes=epub_bytes,
                used_chapter_names=used_chapter_names,
                used_image_names=used_image_names,
                image_hash_to_name=image_hash_to_name,
                remove_all_images=remove_all_images,
            )

            if not chapters:
                skipped.append((entry.filename, "No usable chapter files found"))
                log_warning(f"Skipped {entry.filename}: No usable chapter files found")
                continue

            final_chapters.extend(chapters)

            if not remove_all_images:
                final_images.update(images)

                image_payload_size = sum(len(data) for data in final_images.values())

                if image_payload_size > int(max_output_bytes * IMAGE_SIZE_ABORT_RATIO):
                    raise OutputTooLargeError(
                        "Compilation aborted before reconstructing remaining EPUBs.\n"
                        "Images alone are near or above the Discord upload limit.\n"
                        f"Image payload: {format_bytes(image_payload_size)}\n"
                        f"Limit: {format_bytes(max_output_bytes)}\n"
                        "Try again with `Remove all images` enabled."
                    )

            estimated_size = estimate_compiled_epub_bytes(
                final_chapters=final_chapters,
                final_images=final_images,
                remove_all_images=remove_all_images,
            )

            if estimated_size > max_output_bytes:
                raise OutputTooLargeError(
                    "Compilation aborted before reconstructing remaining EPUBs.\n"
                    f"Estimated output size after EPUB "
                    f"{entry_index}/{len(selected)}: {entry.filename}\n"
                    f"Estimated size: {format_bytes(estimated_size)}\n"
                    f"Limit: {format_bytes(max_output_bytes)}\n"
                    "Try selecting fewer EPUBs or enable `Remove all images`."
                )

        except OutputTooLargeError:
            raise
        except zipfile.BadZipFile:
            skipped.append((entry.filename, "Invalid archived EPUB/ZIP"))
            log_warning(f"Skipped {entry.filename}: Invalid archived EPUB/ZIP")
        except KeyError as exc:
            skipped.append((entry.filename, f"Missing file: {exc}"))
            log_warning(f"Skipped {entry.filename}: Missing file: {exc}")
        except Exception as exc:
            skipped.append((entry.filename, str(exc)[:200]))
            log_warning(f"Skipped {entry.filename}: {exc}")
            if DEBUG_LOGS:
                traceback.print_exc()

    if not final_chapters:
        return None, skipped

    output = await asyncio.to_thread(
        build_compiled_epub_bytes,
        title=title,
        author=author,
        final_chapters=final_chapters,
        final_images=final_images,
        remove_all_images=remove_all_images,
    )

    if len(output) > max_output_bytes:
        raise OutputTooLargeError(
            "The compiled EPUB is too large to send through Discord.\n"
            f"Compiled size: {format_bytes(len(output))}\n"
            f"Limit: {format_bytes(max_output_bytes)}\n"
            "Try selecting fewer EPUBs or enable `Remove all images`."
        )

    return output, skipped

