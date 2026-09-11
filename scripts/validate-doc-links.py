#!/usr/bin/env python3
"""Validate local Markdown file links and heading anchors."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
VENDORED_DOCS = ROOT / "docs" / "codexbar-docs"
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)


def slugify(title: str) -> str:
    title = re.sub(r"<[^>]+>", "", title).strip().lower()
    title = re.sub(r"[^\w\- ]", "", title, flags=re.UNICODE)
    return re.sub(r"[\s]+", "-", title)


def anchors(path: Path) -> set[str]:
    found: set[str] = set()
    counts: dict[str, int] = {}
    for title in HEADING.findall(path.read_text()):
        base = slugify(title)
        count = counts.get(base, 0)
        counts[base] = count + 1
        found.add(base if count == 0 else f"{base}-{count}")
    return found


def main() -> int:
    errors: list[str] = []
    markdown_files = sorted(
        path for path in ROOT.rglob("*.md") if VENDORED_DOCS not in path.parents
    )
    for source in markdown_files:
        text = source.read_text()
        if "file://" in text:
            errors.append(f"{source.relative_to(ROOT)}: file:// link is not portable")
        for raw_target in LINK.findall(text):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            file_part, separator, fragment = target.partition("#")
            target_file = source if not file_part else (source.parent / unquote(file_part)).resolve()
            try:
                target_file.relative_to(ROOT)
            except ValueError:
                errors.append(
                    f"{source.relative_to(ROOT)}: link escapes project: {raw_target}"
                )
                continue
            if not target_file.is_file():
                errors.append(
                    f"{source.relative_to(ROOT)}: missing file: {raw_target}"
                )
                continue
            if separator and fragment and target_file.suffix.lower() == ".md":
                if unquote(fragment).lower() not in anchors(target_file):
                    errors.append(
                        f"{source.relative_to(ROOT)}: missing anchor: {raw_target}"
                    )

    if errors:
        print("\n".join(errors), file=sys.stderr)
        print(f"{len(errors)} error(s)", file=sys.stderr)
        return 1
    print(
        f"Validated {len(markdown_files)} project-authored Markdown files: 0 errors "
        "(verbatim CodexBar snapshot excluded)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
