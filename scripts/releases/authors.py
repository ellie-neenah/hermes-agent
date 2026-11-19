"""Map commit authors to GitHub logins for release notes and attribution checks.

``AUTHOR_MAP`` merges the frozen ``LEGACY_AUTHOR_MAP`` (authors_legacy.py) with
legacy email-named files and hash-named JSON records under
``contributors/emails/``; contributor records win on duplicates.
"""
from __future__ import annotations

import re
from pathlib import Path

from scripts.contributor_mappings import load_safe_mappings
from scripts.releases.authors_legacy import LEGACY_AUTHOR_MAP

REPO_ROOT = Path(__file__).resolve().parents[2]

# ──────────────────────────────────────────────────────────────────────
# Directory-based mappings under contributors/emails/
# ──────────────────────────────────────────────────────────────────────
CONTRIBUTORS_EMAILS_DIR = REPO_ROOT / "contributors" / "emails"


def _load_contributor_dir(directory: Path | None = None) -> dict[str, str]:
    """Load legacy email-named files and hash-named JSON mappings.

    Legacy filenames remain readable. New mappings use a SHA-256 filename
    and store the email and login in the JSON record.
    """
    directory = directory or CONTRIBUTORS_EMAILS_DIR
    mapping = {}
    if not directory.is_dir():
        return load_safe_mappings(directory)
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    mapping[path.name] = line.lstrip("@")
                    break
        except OSError:
            continue
    return {**mapping, **load_safe_mappings(directory)}


# Effective map: frozen legacy dict + contributor records (records win).
AUTHOR_MAP = {**LEGACY_AUTHOR_MAP, **_load_contributor_dir()}


def resolve_author(name: str, email: str) -> str:
    """Resolve a git author to a GitHub @mention."""
    # Try email lookup first
    gh_user = AUTHOR_MAP.get(email)
    if gh_user:
        return f"@{gh_user}"

    # Try noreply pattern
    noreply_match = re.match(r"(\d+)\+(.+)@users\.noreply\.github\.com", email)
    if noreply_match:
        return f"@{noreply_match.group(2)}"

    # Try username@users.noreply.github.com
    noreply_match2 = re.match(r"(.+)@users\.noreply\.github\.com", email)
    if noreply_match2:
        return f"@{noreply_match2.group(1)}"

    # Fallback to git name
    return name
