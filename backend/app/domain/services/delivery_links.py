"""Reconcile local download links against delivered bytes, without reviewing prose.

This is an attachment availability check only. External citations and the
author's conclusions are left intact. It must never call a model or sandbox.
"""
from __future__ import annotations

import html
from pathlib import PurePosixPath
import re
from urllib.parse import unquote, urlsplit


def reconcile_delivery_links(text: str, files, *, known_paths=()) -> str:
    """Keep authorized historical links without counting them as new files."""
    result, prose, fence = [], [], None

    def flush():
        if prose:
            result.append(_reconcile_markup("".join(prose), files, known_paths=known_paths))
            prose.clear()

    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence is not None:
            result.append(line)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
        elif marker:
            flush()
            fence = marker[1]
            result.append(line)
        else:
            prose.append(line)
    flush()
    return "".join(result)


def _reconcile_markup(text: str, files, *, known_paths) -> str:
    paths = {item.file_path for item in files if item.file_id and item.file_path} | set(known_paths)
    ids = {item.file_id for item in files if item.file_id}
    names = {PurePosixPath(path).name for path in paths}

    def unavailable(target: str) -> bool:
        target = unquote(html.unescape(target.strip().strip("<>")))
        try:
            parsed = urlsplit(target)
        except ValueError:
            # A malformed citation is not permission to fail the analysis.
            # Browser URL/scheme sanitization remains a separate boundary.
            return False
        # Public source references are not generated attachment assertions.
        if parsed.scheme in {"https", "http", "mailto"} or parsed.netloc:
            return False
        path = parsed.path
        if parsed.scheme in {"sandbox", "file"} or path.startswith("/home/ubuntu/output/"):
            return path not in paths
        match = re.fullmatch(r"/(?:api/v1/)?files/([^/]+)(?:/(?:download|content))?", path)
        if match:
            return match[1] not in ids
        if not parsed.scheme and not path.startswith(("/", "#")) and PurePosixPath(path).suffix:
            # Relative artifact names are resolved by the preview UI; do not
            # turn a missing file into a clickable download there.
            return path not in names and "/home/ubuntu/output/" + path.removeprefix("./") not in paths
        return False

    def label(value: str, target: str) -> str:
        name = value or PurePosixPath(unquote(target).split("?", 1)[0]).name or "文件"
        return name + "（文件未交付）"

    # Inline links/images, including angle destinations and parentheses in a
    # filename. A missing image becomes readable text rather than a broken img.
    inline = re.compile(r'!?\[([^\]\n]*)\]\(\s*(<[^>\n]+>|(?:\\.|[^()\s]|\([^()\n]*\))+)(?:\s+["\'][^\n]*?["\'])?\s*\)')
    text = inline.sub(lambda m: label(m[1], m[2]) if unavailable(m[2]) else m[0], text)

    # Reference-style links must lose their definition as well as the clickable
    # use. Do not expose the private path as a leftover definition line.
    rejected = set()
    definitions = re.compile(r'^ {0,3}\[([^\]\n]+)\]:\s*(<[^>\n]+>|\S+)(?:\s+[^\n]*)?$', re.MULTILINE)

    def definition(match):
        if unavailable(match[2]):
            rejected.add(" ".join(match[1].casefold().split()))
            return ""
        return match[0]

    text = definitions.sub(definition, text)
    reference = re.compile(r'!?\[([^\]\n]+)\](?:\[([^\]\n]*)\])?')
    text = reference.sub(lambda m: label(m[1], "") if " ".join((m[2] or m[1]).casefold().split()) in rejected else m[0], text)

    # Renderers that allow HTML still cannot publish unavailable local files.
    anchor = re.compile(r'<a\b[^>]*\bhref\s*=\s*(["\'])(.*?)\1[^>]*>(.*?)</a\s*>', re.IGNORECASE | re.DOTALL)
    text = anchor.sub(lambda m: label(m[3], m[2]) if unavailable(m[2]) else m[0], text)
    image = re.compile(r'<img\b[^>]*\bsrc\s*=\s*(["\'])(.*?)\1[^>]*>', re.IGNORECASE | re.DOTALL)
    text = image.sub(lambda m: label("", m[2]) if unavailable(m[2]) else m[0], text)
    return text
