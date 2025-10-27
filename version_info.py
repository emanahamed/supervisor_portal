"""Version & Changelog Utilities.

Provides:
 - VERSION constant (semantic version string MAJOR.MINOR.PATCH)
 - get_changelog(): raw VERSION.md contents (for existing template/modal usage)
 - parse_changelog(): structured list of entries [{'version','date','body','lines'}]
 - latest_entry(): convenience accessor for current VERSION metadata

Design Notes:
The parser is intentionally forgiving – any heading beginning with '## ' and a
token that looks like a version (digits & dots) is treated as a new entry. The
date component (if present) is parsed from the remainder of the heading after a
hyphen. Body lines are preserved verbatim for maximum fidelity (no attempt to
categorise by conventional keep-a-changelog sections at this stage).

Future Enhancements (non‑breaking):
 - Categorise bullets into Added / Changed / Fixed groups if prefixed.
 - Support pre-release identifiers and build metadata.
 - Generate markdown from structured JSON for release automation.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional

VERSION = "2.1.2"

_VERSION_FILE_NAME = 'VERSION.md'


def _version_file_path() -> str:
    return os.path.join(os.path.dirname(__file__), _VERSION_FILE_NAME)


def get_changelog() -> str:
    """Return the raw changelog markdown or a fallback string."""
    path = _version_file_path()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception:
        return "Version history not available."


@dataclass(slots=True)
class ChangelogEntry:
    version: str
    date: Optional[str]
    body: str  # raw body block (markdown) excluding the heading
    lines: List[str]  # individual lines (no trailing newlines)

    def to_dict(self) -> dict:
        return {
            'version': self.version,
            'date': self.date,
            'body': self.body,
            'lines': self.lines,
        }


_HEADING_PATTERN = re.compile(r'^##\s+([0-9]+(?:\.[0-9]+)*)\s*(?:-\s*(.*))?$')


def parse_changelog(markdown: str | None = None) -> List[ChangelogEntry]:
    """Parse the VERSION.md into structured entries.

    The changelog format assumed:
        ## 1.2.3 - 2025-10-08
        (body lines...)

    Date section is optional. Body continues until next '## ' heading or EOF.
    Non-conforming headings are ignored (providing forwards compatibility for
    the top-level '# Version History').
    """
    if markdown is None:
        markdown = get_changelog()
    if not markdown:
        return []
    lines = markdown.splitlines()
    entries: List[ChangelogEntry] = []
    current_version: Optional[str] = None
    current_date: Optional[str] = None
    body_acc: List[str] = []

    def _flush():
        if current_version is not None:
            # Trim leading blank lines in body
            trimmed = list(body_acc)
            while trimmed and trimmed[0].strip() == '':
                trimmed.pop(0)
            while trimmed and trimmed[-1].strip() == '':
                trimmed.pop()
            body_text = '\n'.join(trimmed)
            entries.append(ChangelogEntry(current_version, current_date, body_text, trimmed))

    for line in lines:
        if line.startswith('## '):
            m = _HEADING_PATTERN.match(line)
            if m:
                # New entry
                _flush()
                current_version = m.group(1)
                current_date = m.group(2).strip() if m.group(2) else None
                body_acc = []
                continue
        # Accumulate body if we have started an entry
        if current_version is not None:
            body_acc.append(line)

    _flush()
    return entries


def latest_entry() -> Optional[ChangelogEntry]:
    """Return the entry matching the current VERSION constant, if present."""
    for entry in parse_changelog():
        if entry.version == VERSION:
            return entry
    return None


def changelog_json(limit: int | None = None) -> List[dict]:
    """Return JSON-serialisable list of entries (newest first)."""
    entries = parse_changelog()
    # Entries are in file order newest first in current file; preserve order.
    if limit is not None:
        entries = entries[:limit]
    return [e.to_dict() for e in entries]

