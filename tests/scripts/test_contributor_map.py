"""Tests for the contributor mapping system.

New mappings use hash-named JSON records while existing email-named records
remain readable. The legacy AUTHOR_MAP dict is frozen.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if __package__ in (None, ""):
    sys.path.insert(0, str(REPO_ROOT))

from scripts import add_contributor, audit_pr_attribution  # noqa: E402
from scripts.ci import check_contributor_emails  # noqa: E402
from scripts.contributor_mappings import safe_mapping_path  # noqa: E402
from scripts.releases import authors, authors_legacy  # noqa: E402

# ── directory loader behavior ─────────────────────────────────────────


def test_loader_reads_login_from_first_noncomment_line(tmp_path):
    d = tmp_path / "emails"
    d.mkdir()
    (d / "jane@example.com").write_text("# salvage PR #1\njanedoe\n# trailing note\n")
    mapping = authors._load_contributor_dir(d)
    assert mapping == {"jane@example.com": "janedoe"}


def test_effective_map_merges_legacy_and_directory():
    # Invariant: every legacy entry survives into the effective map unless
    # shadowed by a directory entry, and the directory contributes on top.
    assert set(authors_legacy.LEGACY_AUTHOR_MAP) <= (
        set(authors.AUTHOR_MAP) | set(authors._load_contributor_dir())
    )
    for email, login in authors._load_contributor_dir().items():
        assert authors.AUTHOR_MAP[email] == login


# ── add_contributor.py CLI behavior ───────────────────────────────────


@pytest.fixture()
def emails_dir(tmp_path, monkeypatch):
    d = tmp_path / "contributors" / "emails"
    monkeypatch.setattr(add_contributor, "EMAILS_DIR", d)
    return d


def test_add_creates_mapping_file(emails_dir):
    email = "generated+new@example.invalid"
    rc = add_contributor.add_contributor(email, "newperson", "PR #999 salvage")
    assert rc == 0
    path = safe_mapping_path(emails_dir, email)
    assert path.is_file()
    assert not (emails_dir / email).exists()
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record == {
        "email": email,
        "login": "newperson",
        "comment": "PR #999 salvage",
    }
    assert path.parent.name == "by-hash"
    assert len(path.stem) == 64


def test_safe_record_is_read_by_release_and_attribution_checks(emails_dir, monkeypatch):
    email = "generated+audit@example.invalid"
    monkeypatch.chdir(emails_dir.parent.parent)
    with monkeypatch.context() as script_checkout:
        script_checkout.setattr(
            check_contributor_emails, "AUTHOR_MAP", {email: "auditperson"}, raising=False
        )
        assert not check_contributor_emails.is_mapped(email, "")
        assert check_contributor_emails.is_mapped(email, f'{{"{email}": "auditperson"}}')

    assert add_contributor.add_contributor(email, "auditperson") == 0
    contributor_map = authors._load_contributor_dir(emails_dir)
    assert contributor_map[email] == "auditperson"

    effective_map = {**authors_legacy.LEGACY_AUTHOR_MAP, **contributor_map}
    monkeypatch.setattr(authors, "AUTHOR_MAP", effective_map)
    monkeypatch.setattr(audit_pr_attribution, "AUTHOR_MAP", effective_map, raising=False)
    assert authors.resolve_author("Generated Author", email) == "@auditperson"
    assert audit_pr_attribution.is_mapped(email)
    assert check_contributor_emails.is_mapped(email, "")


def test_guard_accepts_empty_legacy_mapping_file(emails_dir, monkeypatch):
    email = "empty-legacy@example.invalid"
    monkeypatch.chdir(emails_dir.parent.parent)
    emails_dir.mkdir(parents=True)
    (emails_dir / email).touch()

    assert check_contributor_emails.is_mapped(email, "")


def test_add_refuses_login_conflicting_with_legacy_map(emails_dir):
    email, login = next(iter(authors_legacy.LEGACY_AUTHOR_MAP.items()))
    assert add_contributor.add_contributor(email, login + "x") == 1
    assert not (emails_dir / email).exists()
    assert not safe_mapping_path(emails_dir, email).exists()


def test_add_accepts_legacy_consecutive_hyphen_login(emails_dir):
    email = "generated+legacy@example.invalid"
    assert add_contributor.add_contributor(email, "Legacy--Login") == 0
    record = json.loads(safe_mapping_path(emails_dir, email).read_text(encoding="utf-8"))
    assert record["login"] == "Legacy--Login"


def test_add_strips_at_prefix(emails_dir):
    email = "z@example.invalid"
    assert add_contributor.add_contributor(email, "@zeta") == 0
    assert authors._load_contributor_dir(emails_dir)[email] == "zeta"


def test_cli_entrypoint_end_to_end(tmp_path):
    # Run the real script in a subprocess against a temp repo layout.
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("add_contributor.py",):
        # Explicit encoding: add_contributor.py contains UTF-8 multi-byte
        # characters (an em dash), so the locale-default read_text() raises
        # UnicodeDecodeError on non-UTF-8 Windows locales (e.g. cp950).
        (scripts / name).write_text(
            (SCRIPTS_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    (scripts / "contributor_mappings.py").write_text(
        (SCRIPTS_DIR / "contributor_mappings.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    # Minimal stub author map so the legacy lookup import works
    (scripts / "releases").mkdir()
    (scripts / "releases" / "authors_legacy.py").write_text("LEGACY_AUTHOR_MAP = {}\n")
    proc = subprocess.run(
        [sys.executable, str(scripts / "add_contributor.py"),
         "cli@example.com", "cliperson", "via subprocess"],
        cwd=tmp_path, capture_output=True, text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = safe_mapping_path(
        tmp_path / "contributors" / "emails", "cli@example.com"
    )
    assert json.loads(out.read_text(encoding="utf-8"))["login"] == "cliperson"


# ── compatibility with existing email-named records ───────────────────


def test_add_contributor_refuses_case_collision_even_for_same_login(emails_dir, capsys):
    # Same login, different spelling: still refused — the problem is the
    # filename pair, not the login. The exact spelling is what's "present".
    emails_dir.mkdir(parents=True)
    (emails_dir / "Foo@Example.com").write_text("foouser\n")

    assert add_contributor.add_contributor("foo@example.com", "foouser") == 1
    assert "Foo@Example.com" in capsys.readouterr().err
    assert sorted(p.name for p in emails_dir.iterdir()) == ["Foo@Example.com"]
    # Exact-case re-add is the ordinary idempotent path.
    assert add_contributor.add_contributor("Foo@Example.com", "foouser") == 0


def test_case_collision_uses_casefold(emails_dir):
    # casefold, not lower: matches how macOS/Windows fold non-ASCII (ß ~ ss).
    emails_dir.mkdir(parents=True)
    (emails_dir / "strasse@example.com").write_text("someone\n")
    assert add_contributor.add_contributor("STRASSE@example.com", "someone") == 1
    assert add_contributor.add_contributor("straße@example.com", "someone") == 1
