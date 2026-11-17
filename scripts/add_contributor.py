#!/usr/bin/env python3
"""Add a contributor email → GitHub login mapping.

Writes one JSON file per email under contributors/emails/by-hash/, named by
the SHA-256 digest of the email. The legacy AUTHOR_MAP dict in
scripts/releases/authors_legacy.py is frozen — do not append to it.

Usage (from the repo root):
    python3 scripts/add_contributor.py <email> <github-login> [comment...]

    # e.g.
    python3 scripts/add_contributor.py contributor@example.invalid login "PR #12345"

Idempotent: if the mapping already exists with the same login, prints
"present" and exits 0. If the email maps to a DIFFERENT login (here or in the
legacy AUTHOR_MAP), refuses with exit 1 so a typo can't silently reassign
someone's commits.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EMAILS_DIR = REPO_ROOT / "contributors" / "emails"

if __package__ in (None, ""):
    sys.path.insert(0, str(REPO_ROOT))

from scripts.contributor_mappings import (  # noqa: E402
    read_safe_mapping,
    safe_mapping_path,
    write_safe_mapping,
)

_EMAIL_RE = re.compile(r"^[^/\\\s]+@[^/\\\s]+$")
# Accept legacy logins with consecutive hyphens while enforcing the length
# and character constraints used for GitHub usernames.
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


def read_mapping_file(path: Path) -> str | None:
    """Return the login from a mapping file (first non-comment line)."""
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    except OSError:
        pass
    return None


def _legacy_login(email: str) -> str | None:
    """Look the email up in the frozen legacy AUTHOR_MAP in scripts/releases/authors_legacy.py."""
    from scripts.releases.authors_legacy import LEGACY_AUTHOR_MAP

    return LEGACY_AUTHOR_MAP.get(email)


def _case_collision(email: str) -> str | None:
    """A legacy mapping filename that differs from `email` only in case.

    Returns the colliding filename, or None. Exact matches are not collisions --
    that is the ordinary "already mapped" path handled by the caller.
    """
    if not EMAILS_DIR.is_dir():
        return None

    # casefold (not lower) matches how macOS/Windows fold non-ASCII text —
    # same key scripts/check-case-collisions.py uses repo-wide.
    folded = email.casefold()
    for entry in EMAILS_DIR.iterdir():
        if entry.name != email and entry.name.casefold() == folded:
            return entry.name
    return None


def add_contributor(email: str, login: str, comment: str = "") -> int:
    email = email.strip()
    login = login.strip().lstrip("@")

    if not _EMAIL_RE.match(email):
        print(f"error: {email!r} does not look like a commit-author email", file=sys.stderr)
        return 2
    if not _LOGIN_RE.match(login):
        print(f"error: {login!r} is not a valid GitHub login", file=sys.stderr)
        return 2

    legacy_path = EMAILS_DIR / email
    safe_path = safe_mapping_path(EMAILS_DIR, email)

    # Legacy records use email-keyed filenames, so keep rejecting ambiguous
    # case-folded keys while those records remain supported.
    collision = _case_collision(email)
    if collision is not None:
        print(
            f"error: {email} collides with existing mapping {collision} on "
            "case-insensitive filesystems (Windows/macOS) — the two are the same "
            "file there. Reuse that mapping, or resolve manually.",
            file=sys.stderr,
        )
        return 1

    existing = read_mapping_file(legacy_path) if legacy_path.is_file() else None
    if existing is None:
        existing = read_safe_mapping(safe_path, email)
    if existing is None:
        existing = _legacy_login(email)
    if existing is not None:
        if existing == login:
            print("present")
            return 0
        print(
            f"error: {email} already maps to {existing!r} (asked for {login!r}) — "
            "resolve manually",
            file=sys.stderr,
        )
        return 1

    path = write_safe_mapping(EMAILS_DIR, email, login, comment)
    print(f"added: contributors/emails/{path.relative_to(EMAILS_DIR)} -> {login}")
    return 0


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    email, login = sys.argv[1], sys.argv[2]
    comment = " ".join(sys.argv[3:])
    return add_contributor(email, login, comment)


if __name__ == "__main__":
    sys.exit(main())
