import hashlib
import io
import mimetypes
import posixpath
import re
import urllib.parse
import uuid
import zlib
import zipfile
from typing import Dict, List, Optional, Set, Tuple
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import discord
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from config import (
    ALLOWED_NAME_RE, CONTAINER_NS, DEFAULT_UPLOAD_LIMIT_BYTES,
    EPUB_BASE_OVERHEAD_BYTES, EPUB_EXT_RE, EPUB_NS, EPUB_PER_CHAPTER_OVERHEAD_BYTES,
    EPUB_PER_IMAGE_OVERHEAD_BYTES, MAX_SINGLE_FILE_UNCOMPRESSED_BYTES,
    MAX_SOURCE_UNCOMPRESSED_BYTES, MAX_ZIP_MEMBERS, SAFE_FILE_RE, SAFE_META_RE,
    SVG_NS, XLINK_NS, XHTML_NS, XML_NS,
)

ET.register_namespace("", XHTML_NS)
ET.register_namespace("svg", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)
ET.register_namespace("epub", EPUB_NS)

XML_PARSE_ERRORS = (ET.ParseError, DefusedXmlException, ValueError)
GUIDE_SKIP_TYPES = {"cover", "title-page", "toc"}
TEXT_TAGS = {
    "a",
    "blockquote",
    "dd",
    "div",
    "dt",
    "figcaption",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "p",
    "pre",
    "span",
    "td",
    "th",
}
IMAGE_TAGS = {"img", "image", "svg"}
SAFE_URL_SCHEMES = {"", "http", "https", "mailto"}
URL_ATTRS = {
    "href",
    "src",
    "poster",
    "longdesc",
    "cite",
    "action",
    "formaction",
    "background",
}
RESOURCE_URL_ATTRS = {
    "src",
    "poster",
    "longdesc",
    "background",
}
RESOURCE_URL_TAGS = {
    "img",
    "image",
}
DROP_ATTRS = {
    "srcset",
    "style",
    "formaction",
}


def local_name(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def sanitize_output_name(raw: str) -> str:
    raw = raw.strip().strip(".")
    if not raw:
        return "compiled"

    if not ALLOWED_NAME_RE.fullmatch(raw):
        raise ValueError(
            "Only letters, numbers, spaces, dash, underscore, comma, "
            "period, apostrophe, and parentheses are allowed."
        )

    return raw[:120] or "compiled"


def safe_default_output_name(raw: str) -> str:
    try:
        return sanitize_output_name(raw)
    except ValueError:
        clean = SAFE_FILE_RE.sub("_", raw or "").strip("._ ")
        return clean[:120] or "compiled"


def sanitize_author(raw: str) -> str:
    raw = SAFE_META_RE.sub(" ", raw or "").strip()
    raw = "".join(ch for ch in raw if ch.isprintable())
    return raw[:120] or "Discord Channel"


def sanitize_internal_name(name: str, default_stem: str) -> str:
    base = posixpath.basename(name.replace("\\", "/")).strip()

    if not base:
        base = default_stem

    if "." in base:
        stem, ext = base.rsplit(".", 1)
        ext = "." + SAFE_FILE_RE.sub("", ext.lower())
    else:
        stem, ext = base, ""

    stem = SAFE_FILE_RE.sub("_", stem).strip("._")

    if not stem:
        stem = default_stem

    return stem + ext


def safe_output_zip_path(prefix: str, filename: str) -> str:
    clean = sanitize_internal_name(filename, "file")

    if "/" in clean or "\\" in clean:
        raise ValueError(f"Unsafe output filename: {filename}")

    return f"{prefix.rstrip('/')}/{clean}"


def make_unique_name(name: str, used: Set[str]) -> str:
    if name not in used:
        used.add(name)
        return name

    if "." in name:
        stem, ext = name.rsplit(".", 1)
        ext = "." + ext
    else:
        stem, ext = name, ""

    i = 2

    while True:
        candidate = f"{stem}_{i}{ext}"

        if candidate not in used:
            used.add(candidate)
            return candidate

        i += 1


def resolve_href(base_path: str, href: str) -> str:
    clean = href.split("#", 1)[0].strip()
    clean = urllib.parse.unquote(clean)

    if not clean:
        raise ValueError("Empty href")

    if "\x00" in clean:
        raise ValueError("Invalid href")

    clean = clean.replace("\\", "/")

    resolved = posixpath.normpath(
        posixpath.join(posixpath.dirname(base_path), clean)
    )

    if resolved.startswith("/") or resolved == ".." or resolved.startswith("../"):
        raise ValueError(f"Unsafe href: {href}")

    if any(part == ".." for part in resolved.split("/")):
        raise ValueError(f"Unsafe href: {href}")

    return resolved


def is_epub_attachment(att: discord.Attachment) -> bool:
    return bool(att.filename and EPUB_EXT_RE.search(att.filename))


def has_manifest_property(props: str, prop: str) -> bool:
    return prop in {p.strip().lower() for p in (props or "").split()}


def looks_like_structural_page_by_name(href: str, manifest_props: str) -> bool:
    name = posixpath.basename(href.lower())

    if has_manifest_property(manifest_props, "nav"):
        return True

    structural_names = {
        "nav.xhtml",
        "nav.html",
        "toc.xhtml",
        "toc.html",
        "toc.ncx",
        "cover.xhtml",
        "cover.html",
        "titlepage.xhtml",
        "titlepage.html",
        "title-page.xhtml",
        "title-page.html",
    }

    return name in structural_names


def resolve_upload_limit_bytes(interaction: discord.Interaction) -> int:
    limit = getattr(interaction, "attachment_size_limit", None)

    if isinstance(limit, int) and limit > 0:
        return limit

    return DEFAULT_UPLOAD_LIMIT_BYTES


def estimate_compiled_epub_bytes(
    final_chapters: List[Tuple[str, str, bytes]],
    final_images: Dict[str, bytes],
    remove_all_images: bool,
) -> int:
    chapter_part = sum(raw_deflate_size(blob) for _, _, blob in final_chapters)
    image_part = (
        0
        if remove_all_images
        else sum(raw_deflate_size(data) for data in final_images.values())
    )
    image_count = 0 if remove_all_images else len(final_images)

    return (
        chapter_part
        + image_part
        + EPUB_BASE_OVERHEAD_BYTES
        + (len(final_chapters) * EPUB_PER_CHAPTER_OVERHEAD_BYTES)
        + (image_count * EPUB_PER_IMAGE_OVERHEAD_BYTES)
    )


def raw_deflate_size(data: bytes) -> int:
    compressor = zlib.compressobj(level=6, wbits=-15)
    return len(compressor.compress(data) + compressor.flush())


def format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)

    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"

            return f"{value:.1f} {unit}"

        value /= 1024

    return f"{size} B"


def get_text_content(elem: Optional[ET.Element]) -> str:
    if elem is None:
        return ""

    return " ".join("".join(elem.itertext()).split()).strip()


def parse_xml(data: bytes) -> ET.Element:
    return SafeET.fromstring(data)


def safe_zip_read(zf: zipfile.ZipFile, name: str) -> bytes:
    info = zf.getinfo(name)

    if info.file_size > MAX_SINGLE_FILE_UNCOMPRESSED_BYTES:
        raise ValueError(f"File too large inside EPUB: {name}")

    return zf.read(name)


def validate_zip_member_names(zf: zipfile.ZipFile) -> None:
    members = zf.infolist()

    if len(members) > MAX_ZIP_MEMBERS:
        raise ValueError("EPUB has too many files")

    for info in members:
        name = info.filename.replace("\\", "/")

        if "\x00" in name:
            raise ValueError(f"Invalid EPUB path: {info.filename}")

        if name.startswith("/"):
            raise ValueError(f"Unsafe absolute EPUB path: {info.filename}")

        parts = name.split("/")

        if any(part == ".." for part in parts):
            raise ValueError(f"Unsafe EPUB path traversal: {info.filename}")


def validate_zip_sizes(zf: zipfile.ZipFile) -> None:
    total = 0
    members = zf.infolist()

    if len(members) > MAX_ZIP_MEMBERS:
        raise ValueError("EPUB has too many files")

    for info in members:
        if info.file_size > MAX_SINGLE_FILE_UNCOMPRESSED_BYTES:
            raise ValueError(f"EPUB member too large: {info.filename}")

        total += info.file_size

        if total > MAX_SOURCE_UNCOMPRESSED_BYTES:
            raise ValueError("EPUB uncompressed content is too large")


def validate_epub_basics(zf: zipfile.ZipFile) -> None:
    names = set(zf.namelist())

    if "mimetype" not in names:
        raise ValueError("Not a valid EPUB: missing mimetype")

    try:
        mimetype_data = zf.read("mimetype")
    except KeyError:
        raise ValueError("Not a valid EPUB: missing mimetype")

    if mimetype_data.strip() != b"application/epub+zip":
        raise ValueError("Not a valid EPUB: bad mimetype")

    if "META-INF/container.xml" not in names:
        raise ValueError("Not a valid EPUB: missing container.xml")


def find_container_rootfile(zf: zipfile.ZipFile) -> str:
    data = safe_zip_read(zf, "META-INF/container.xml")
    root = parse_xml(data)
    rootfile = root.find(".//c:rootfile", CONTAINER_NS)

    if rootfile is None:
        raise ValueError("Missing rootfile in container.xml")

    path = rootfile.get("full-path")

    if not path:
        raise ValueError("container.xml rootfile missing full-path")

    return path


def parse_opf(
    zf: zipfile.ZipFile,
    opf_path: str,
) -> Tuple[str, Dict[str, dict], List[str], Set[str]]:
    data = safe_zip_read(zf, opf_path)
    root = parse_xml(data)
    opf_dir = posixpath.dirname(opf_path)

    manifest = {}
    spine = []
    structural_hrefs_to_skip: Set[str] = set()

    manifest_elem = None
    spine_elem = None
    guide_elem = None

    for child in root:
        lname = local_name(child.tag)

        if lname == "manifest":
            manifest_elem = child
        elif lname == "spine":
            spine_elem = child
        elif lname == "guide":
            guide_elem = child

    if manifest_elem is None or spine_elem is None:
        raise ValueError("OPF missing manifest or spine")

    for item in manifest_elem:
        if local_name(item.tag) != "item":
            continue

        item_id = item.get("id")
        href = item.get("href")
        media_type = item.get("media-type", "")
        props = item.get("properties", "")

        if not item_id or not href:
            continue

        full_path = resolve_href(opf_path, href)
        manifest[item_id] = {
            "href": full_path,
            "media_type": media_type,
            "properties": props,
        }

    for itemref in spine_elem:
        if local_name(itemref.tag) != "itemref":
            continue

        idref = itemref.get("idref")

        if idref:
            spine.append(idref)

    if guide_elem is not None:
        for ref in guide_elem:
            if local_name(ref.tag) != "reference":
                continue

            ref_type = (ref.get("type") or "").strip().lower()
            href = ref.get("href")

            if ref_type not in GUIDE_SKIP_TYPES or not href:
                continue

            try:
                full_path = resolve_href(opf_path, href)
            except ValueError:
                continue

            if not (
                full_path.startswith("../")
                or full_path == ".."
                or full_path.startswith("/")
            ):
                structural_hrefs_to_skip.add(full_path)

    return opf_dir, manifest, spine, structural_hrefs_to_skip


def find_first_by_local_name(root: ET.Element, name: str) -> Optional[ET.Element]:
    for elem in root.iter():
        if local_name(elem.tag) == name:
            return elem

    return None


def get_document_namespace(root: ET.Element) -> str:
    if root.tag.startswith("{") and "}" in root.tag:
        return root.tag[1:].split("}", 1)[0]

    return XHTML_NS


def find_head(root: ET.Element) -> Optional[ET.Element]:
    for elem in root.iter():
        if local_name(elem.tag) == "head":
            return elem

    return None


def extract_title_from_xhtml(root: ET.Element) -> str:
    for name in ("h1", "h2", "title"):
        elem = find_first_by_local_name(root, name)
        text = get_text_content(elem)

        if text:
            return text[:120]

    return ""


def remove_stylesheet_links_and_add_main(root: ET.Element) -> None:
    ns = get_document_namespace(root)
    head = find_head(root)

    if head is None:
        head = ET.Element(f"{{{ns}}}head")
        root.insert(0, head)

    to_remove = []

    for child in list(head):
        if local_name(child.tag) != "link":
            continue

        rel = (child.get("rel") or "").lower()

        if "stylesheet" in rel:
            to_remove.append(child)

    for child in to_remove:
        head.remove(child)

    link = ET.Element(f"{{{ns}}}link")
    link.set("rel", "stylesheet")
    link.set("type", "text/css")
    link.set("href", "../styles/main.css")
    head.append(link)


def gather_and_rewrite_images(
    root: ET.Element,
    chapter_path: str,
    zf: zipfile.ZipFile,
    local_image_path_map: Dict[str, str],
    image_hash_to_name: Dict[str, str],
    used_image_names: Set[str],
) -> Dict[str, bytes]:
    collected: Dict[str, bytes] = {}

    for elem in root.iter():
        tag = local_name(elem.tag)
        attrs = []

        if tag == "img":
            attrs.append("src")
        elif tag == "image":
            attrs.extend([f"{{{XLINK_NS}}}href", "href"])

        for attr in attrs:
            val = elem.get(attr)

            if not val:
                continue

            lower_val = val.strip().lower()

            if (
                lower_val.startswith("data:")
                or lower_val.startswith("http://")
                or lower_val.startswith("https://")
                or lower_val.startswith("//")
            ):
                continue

            try:
                full = resolve_href(chapter_path, val)
            except ValueError:
                continue

            if full not in local_image_path_map:
                try:
                    image_bytes = safe_zip_read(zf, full)
                except KeyError:
                    continue

                digest = hashlib.sha256(image_bytes).hexdigest()

                if digest in image_hash_to_name:
                    new_name = image_hash_to_name[digest]
                else:
                    original_name = sanitize_internal_name(
                        posixpath.basename(full),
                        "image",
                    )
                    new_name = make_unique_name(original_name, used_image_names)
                    image_hash_to_name[digest] = new_name
                    collected[new_name] = image_bytes

                local_image_path_map[full] = new_name

            elem.set(attr, f"../images/{local_image_path_map[full]}")

    return collected


def remove_all_images_from_xhtml(root: ET.Element) -> None:
    def build_parent_map(root_elem: ET.Element) -> Dict[ET.Element, ET.Element]:
        return {child: parent for parent in root_elem.iter() for child in parent}

    def has_meaningful_text(elem: ET.Element) -> bool:
        if (elem.text or "").strip():
            return True

        for child in elem:
            if (child.tail or "").strip():
                return True

        return False

    def svg_has_non_image_content(elem: ET.Element) -> bool:
        if has_meaningful_text(elem):
            return True

        for child in elem:
            child_name = local_name(child.tag)

            if child_name == "image":
                continue

            if child_name in {"title", "desc"}:
                if "".join(child.itertext()).strip():
                    return True

                continue

            return True

        return False

    parent_map = build_parent_map(root)

    for elem in list(root.iter()):
        if local_name(elem.tag) != "img":
            continue

        parent = parent_map.get(elem)

        if parent is not None:
            parent.remove(elem)

    parent_map = build_parent_map(root)

    for elem in list(root.iter()):
        if local_name(elem.tag) != "image":
            continue

        parent = parent_map.get(elem)

        if parent is not None:
            parent.remove(elem)

    changed = True

    while changed:
        changed = False
        parent_map = build_parent_map(root)

        for elem in list(root.iter()):
            if local_name(elem.tag) != "svg":
                continue

            if svg_has_non_image_content(elem):
                continue

            parent = parent_map.get(elem)

            if parent is not None:
                parent.remove(elem)
                changed = True


def disable_view_items(items: List[discord.ui.Item]) -> None:
    for item in items:
        if hasattr(item, "disabled"):
            item.disabled = True

        children = getattr(item, "children", None)

        if children:
            disable_view_items(list(children))


def strip_dangerous_elements(root: ET.Element) -> None:
    dangerous = {
        "script",
        "iframe",
        "object",
        "embed",
        "foreignObject",
        "audio",
        "video",
        "source",
        "track",
        "form",
        "input",
        "button",
        "select",
        "textarea",
        "meta",
        "base",
    }

    changed = True

    while changed:
        changed = False

        for parent in root.iter():
            for child in list(parent):
                if local_name(child.tag) in dangerous:
                    parent.remove(child)
                    changed = True


def is_safe_url_value(tag_name: str, attr_name: str, value: str) -> bool:
    raw = (value or "").strip()

    if not raw:
        return True

    if raw.startswith("#"):
        return True

    parsed = raw.split(":", 1)

    if len(parsed) == 1:
        return True

    scheme = parsed[0].strip().lower()

    if attr_name in RESOURCE_URL_ATTRS or tag_name in RESOURCE_URL_TAGS:
        return False

    return scheme in SAFE_URL_SCHEMES


def strip_dangerous_attributes(root: ET.Element) -> None:
    for elem in root.iter():
        tag_lname = local_name(elem.tag)

        for attr in list(elem.attrib):
            attr_lname = local_name(attr).lower()
            value = elem.get(attr) or ""

            if attr_lname.startswith("on") or attr_lname in DROP_ATTRS:
                elem.attrib.pop(attr, None)
                continue

            if attr_lname in URL_ATTRS and not is_safe_url_value(tag_lname, attr_lname, value):
                elem.attrib.pop(attr, None)
                continue


def get_meaningful_text_length(root: ET.Element) -> int:
    parts = []

    for elem in root.iter():
        if local_name(elem.tag) in TEXT_TAGS:
            text = " ".join("".join(elem.itertext()).split())

            if text:
                parts.append(text)

    return len(" ".join(parts))


def count_inline_images(root: ET.Element) -> int:
    count = 0

    for elem in root.iter():
        if local_name(elem.tag) in IMAGE_TAGS:
            count += 1

    return count


def count_links(root: ET.Element) -> int:
    count = 0

    for elem in root.iter():
        if local_name(elem.tag) == "a" and elem.get("href"):
            count += 1

    return count


def has_epub_type(root: ET.Element, wanted: Set[str]) -> bool:
    epub_type_attr = f"{{{EPUB_NS}}}type"

    for elem in root.iter():
        raw = elem.get(epub_type_attr) or elem.get("epub:type") or ""
        values = {part.strip().lower() for part in raw.split()}

        if values & wanted:
            return True

    return False


def has_nav_toc_element(root: ET.Element) -> bool:
    for elem in root.iter():
        if local_name(elem.tag) != "nav":
            continue

        epub_type = (
            elem.get(f"{{{EPUB_NS}}}type") or elem.get("epub:type") or ""
        ).lower()
        role = (elem.get("role") or "").lower()

        if "toc" in epub_type or role in {"doc-toc", "navigation"}:
            return True

    return False


def is_probably_toc_page(root: ET.Element) -> bool:
    text_len = get_meaningful_text_length(root)
    link_count = count_links(root)

    if link_count < 12:
        return False

    if text_len >= 1500:
        return False

    link_density = link_count / max(text_len / 500, 1)

    return link_density >= 6


def is_probably_cover_or_title_page(root: ET.Element, href: str) -> bool:
    text_len = get_meaningful_text_length(root)
    image_count = count_inline_images(root)
    name = posixpath.basename(href.lower())

    structural_name_parts = (
        "cover",
        "titlepage",
        "title-page",
    )

    if any(part in name for part in structural_name_parts) and text_len < 500:
        return True

    if has_epub_type(root, {"cover", "titlepage", "title-page"}):
        return True

    if image_count > 0 and text_len < 40:
        return True

    return False


def is_probably_non_chapter_page(root: ET.Element, href: str) -> bool:
    text_len = get_meaningful_text_length(root)
    name = posixpath.basename(href.lower())

    if text_len == 0 and count_inline_images(root) == 0:
        return True

    if has_nav_toc_element(root):
        return True

    if has_epub_type(root, {"toc", "nav", "landmarks", "page-list"}):
        return True

    if is_probably_toc_page(root):
        return True

    if is_probably_cover_or_title_page(root, href):
        return True

    if any(part in name for part in ("toc", "nav")) and text_len < 800:
        return True

    return False


def chapter_media_type(media_type: str) -> bool:
    return media_type in {
        "application/xhtml+xml",
        "text/html",
        "application/xml",
    }


def build_simple_stylesheet() -> bytes:
    css = """
body {
  font-family: serif;
  line-height: 1.45;
  margin: 5%;
}
h1, h2, h3, h4, h5, h6 {
  margin-top: 1.4em;
  margin-bottom: 0.6em;
}
p {
  margin: 0 0 0.9em 0;
}
img, svg {
  max-width: 100%;
}
"""
    return css.strip().encode("utf-8")


def make_nav_xhtml(chapters: List[Tuple[str, str]]) -> bytes:
    ET.register_namespace("", XHTML_NS)
    ET.register_namespace("epub", EPUB_NS)

    html = ET.Element(f"{{{XHTML_NS}}}html")
    html.set(f"{{{XML_NS}}}lang", "en")
    html.set("lang", "en")

    head = ET.SubElement(html, f"{{{XHTML_NS}}}head")
    title = ET.SubElement(head, f"{{{XHTML_NS}}}title")
    title.text = "Table of Contents"

    body = ET.SubElement(html, f"{{{XHTML_NS}}}body")
    nav = ET.SubElement(body, f"{{{XHTML_NS}}}nav")
    nav.set(f"{{{EPUB_NS}}}type", "toc")
    nav.set("id", "toc")

    h1 = ET.SubElement(nav, f"{{{XHTML_NS}}}h1")
    h1.text = "Table of Contents"

    ol = ET.SubElement(nav, f"{{{XHTML_NS}}}ol")

    for href, label in chapters:
        li = ET.SubElement(ol, f"{{{XHTML_NS}}}li")
        a = ET.SubElement(li, f"{{{XHTML_NS}}}a")
        a.set("href", href)
        a.text = label

    ET.indent(html, space="  ")

    return ET.tostring(
        html,
        encoding="utf-8",
        xml_declaration=True,
        method="xml",
    )


def _xml_escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def make_toc_ncx(chapters: List[Tuple[str, str]], uid: str, title_text: str) -> bytes:
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">',
        "  <head>",
        f'    <meta name="dtb:uid" content="{_xml_escape(uid)}" />',
        "  </head>",
        "  <docTitle>",
        f"    <text>{_xml_escape(title_text)}</text>",
        "  </docTitle>",
        "  <navMap>",
    ]

    for i, (href, label) in enumerate(chapters, start=1):
        lines.extend(
            [
                f'    <navPoint id="navPoint-{i}" playOrder="{i}">',
                "      <navLabel>",
                f"        <text>{_xml_escape(label)}</text>",
                "      </navLabel>",
                f'      <content src="{_xml_escape(href)}" />',
                "    </navPoint>",
            ]
        )

    lines.extend(
        [
            "  </navMap>",
            "</ncx>",
            "",
        ]
    )

    return "\n".join(lines).encode("utf-8")


def guess_media_type(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    known = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "svg": "image/svg+xml",
        "webp": "image/webp",
    }

    if ext in known:
        return known[ext]

    guessed, _ = mimetypes.guess_type(filename)

    return guessed or "application/octet-stream"


def make_content_opf(
    uid: str,
    title_text: str,
    author: str,
    chapters: List[Tuple[str, str]],
    image_names: List[str],
) -> bytes:
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<package xmlns="http://www.idpf.org/2007/opf" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'version="3.0" unique-identifier="bookid">',
        "  <metadata>",
        f'    <dc:identifier id="bookid">{_xml_escape(uid)}</dc:identifier>',
        f"    <dc:title>{_xml_escape(title_text)}</dc:title>",
        f"    <dc:creator>{_xml_escape(author)}</dc:creator>",
        "    <dc:language>en</dc:language>",
        f'    <meta property="dcterms:modified">{modified}</meta>',
        "  </metadata>",
        "  <manifest>",
        '    <item id="nav" href="nav.xhtml" '
        'media-type="application/xhtml+xml" properties="nav" />',
        '    <item id="ncx" href="toc.ncx" '
        'media-type="application/x-dtbncx+xml" />',
        '    <item id="style" href="styles/main.css" media-type="text/css" />',
    ]

    for i, (href, _) in enumerate(chapters, start=1):
        lines.append(
            f'    <item id="chap{i}" href="{_xml_escape(href)}" '
            'media-type="application/xhtml+xml" />'
        )

    for i, image_name in enumerate(image_names, start=1):
        media_type = guess_media_type(image_name)
        lines.append(
            f'    <item id="img{i}" href="images/{_xml_escape(image_name)}" '
            f'media-type="{media_type}" />'
        )

    lines.append("  </manifest>")
    lines.append('  <spine toc="ncx">')

    for i in range(1, len(chapters) + 1):
        lines.append(f'    <itemref idref="chap{i}" />')

    lines.extend(
        [
            "  </spine>",
            "</package>",
            "",
        ]
    )

    return "\n".join(lines).encode("utf-8")


def make_container_xml() -> bytes:
    data = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0"
  xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf"
      media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    return data.encode("utf-8")


def extract_book_content(
    epub_bytes: bytes,
    used_chapter_names: Set[str],
    used_image_names: Set[str],
    image_hash_to_name: Dict[str, str],
    remove_all_images: bool = False,
) -> Tuple[List[Tuple[str, str, bytes]], Dict[str, bytes]]:
    chapters_out: List[Tuple[str, str, bytes]] = []
    images_out: Dict[str, bytes] = {}
    local_image_path_map: Dict[str, str] = {}

    with zipfile.ZipFile(io.BytesIO(epub_bytes), "r") as zf:
        validate_zip_member_names(zf)
        validate_zip_sizes(zf)
        validate_epub_basics(zf)

        opf_path = find_container_rootfile(zf)
        _, manifest, spine, structural_hrefs_to_skip = parse_opf(zf, opf_path)

        chapter_index = 1

        for idref in spine:
            item = manifest.get(idref)

            if not item:
                continue

            href = item["href"]
            media_type = item["media_type"]
            props = item["properties"]

            if not chapter_media_type(media_type):
                continue

            if has_manifest_property(props, "nav"):
                continue

            if has_manifest_property(props, "cover-image"):
                continue

            if href in structural_hrefs_to_skip:
                continue

            if looks_like_structural_page_by_name(href, props):
                continue

            try:
                raw = safe_zip_read(zf, href)
            except KeyError:
                continue

            try:
                root = parse_xml(raw)
            except XML_PARSE_ERRORS:
                continue

            if local_name(root.tag) != "html":
                continue

            strip_dangerous_elements(root)
            strip_dangerous_attributes(root)

            if is_probably_non_chapter_page(root, href):
                continue

            remove_stylesheet_links_and_add_main(root)

            if remove_all_images:
                remove_all_images_from_xhtml(root)
            else:
                found_images = gather_and_rewrite_images(
                    root=root,
                    chapter_path=href,
                    zf=zf,
                    local_image_path_map=local_image_path_map,
                    image_hash_to_name=image_hash_to_name,
                    used_image_names=used_image_names,
                )
                images_out.update(found_images)

            chapter_title = extract_title_from_xhtml(root)

            if not chapter_title:
                chapter_title = f"Chapter {len(chapters_out) + 1}"

            original_name = sanitize_internal_name(
                posixpath.basename(href),
                f"chapter_{chapter_index}",
            )

            if not original_name.lower().endswith((".xhtml", ".html", ".htm")):
                original_name += ".xhtml"

            if original_name.lower().endswith((".html", ".htm")):
                original_name = re.sub(r"\.html?$", ".xhtml", original_name)

            unique_name = make_unique_name(original_name, used_chapter_names)
            chapter_zip_path = safe_output_zip_path("text", unique_name)

            ET.register_namespace("", XHTML_NS)
            ET.register_namespace("epub", EPUB_NS)
            ET.register_namespace("svg", SVG_NS)
            ET.register_namespace("xlink", XLINK_NS)

            chapter_bytes = ET.tostring(
                root,
                encoding="utf-8",
                xml_declaration=True,
                method="xml",
            )

            chapters_out.append((chapter_zip_path, chapter_title, chapter_bytes))
            chapter_index += 1

    return chapters_out, images_out


def build_compiled_epub_bytes(
    title: str,
    author: str,
    final_chapters: List[Tuple[str, str, bytes]],
    final_images: Dict[str, bytes],
    remove_all_images: bool,
) -> bytes:
    chapter_toc = [(href, label) for href, label, _ in final_chapters]
    uid = f"urn:uuid:{uuid.uuid4()}"

    with io.BytesIO() as out:
        with zipfile.ZipFile(out, "w") as zf:
            mimetype_info = zipfile.ZipInfo("mimetype")
            mimetype_info.compress_type = zipfile.ZIP_STORED
            zf.writestr(mimetype_info, b"application/epub+zip")

            zf.writestr(
                "META-INF/container.xml",
                make_container_xml(),
                compress_type=zipfile.ZIP_DEFLATED,
            )
            zf.writestr(
                "OEBPS/styles/main.css",
                build_simple_stylesheet(),
                compress_type=zipfile.ZIP_DEFLATED,
            )
            zf.writestr(
                "OEBPS/nav.xhtml",
                make_nav_xhtml(chapter_toc),
                compress_type=zipfile.ZIP_DEFLATED,
            )
            zf.writestr(
                "OEBPS/toc.ncx",
                make_toc_ncx(chapter_toc, uid, title),
                compress_type=zipfile.ZIP_DEFLATED,
            )
            zf.writestr(
                "OEBPS/content.opf",
                make_content_opf(
                    uid=uid,
                    title_text=title,
                    author=author,
                    chapters=chapter_toc,
                    image_names=[] if remove_all_images else sorted(final_images.keys()),
                ),
                compress_type=zipfile.ZIP_DEFLATED,
            )

            for href, _, chapter_bytes in final_chapters:
                zf.writestr(
                    f"OEBPS/{href}",
                    chapter_bytes,
                    compress_type=zipfile.ZIP_DEFLATED,
                )

            if not remove_all_images:
                for image_name, image_bytes in final_images.items():
                    zf.writestr(
                        f"OEBPS/{safe_output_zip_path('images', image_name)}",
                        image_bytes,
                        compress_type=zipfile.ZIP_DEFLATED,
                    )

        return out.getvalue()

