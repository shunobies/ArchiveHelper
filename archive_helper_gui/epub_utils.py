from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def _first_text(root: ET.Element, paths: list[str], ns: dict[str, str]) -> str:
    for path in paths:
        node = root.find(path, ns)
        if node is not None and node.text:
            txt = _clean(node.text)
            if txt:
                return txt
    return ""


def extract_epub_metadata(epub_path: Path) -> dict[str, str]:
    """Best-effort metadata extraction for title/author/year from an EPUB file."""
    metadata = {"title": "", "author": "", "year": ""}
    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            container_raw = zf.read("META-INF/container.xml")
            container_root = ET.fromstring(container_raw)
            ns_container = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
            rootfile = container_root.find(".//c:rootfile", ns_container)
            if rootfile is None:
                return metadata
            opf_path = (rootfile.attrib.get("full-path") or "").strip()
            if not opf_path:
                return metadata

            opf_raw = zf.read(opf_path)
            opf_root = ET.fromstring(opf_raw)
            ns = {
                "opf": "http://www.idpf.org/2007/opf",
                "dc": "http://purl.org/dc/elements/1.1/",
            }

            title = _first_text(opf_root, [".//dc:title", ".//opf:metadata/dc:title"], ns)
            author = _first_text(
                opf_root,
                [
                    ".//dc:creator",
                    ".//opf:metadata/dc:creator",
                    ".//dc:contributor",
                ],
                ns,
            )
            date_text = _first_text(opf_root, [".//dc:date", ".//opf:metadata/dc:date"], ns)
            year_match = re.search(r"(\d{4})", date_text)

            metadata["title"] = title
            metadata["author"] = author
            metadata["year"] = year_match.group(1) if year_match else ""
    except Exception:
        return metadata

    return metadata
