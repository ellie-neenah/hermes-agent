"""Shared storage helpers for contributor mappings."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def safe_mapping_path(directory: Path, email: str) -> Path:
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
    return directory / "by-hash" / f"{digest}.json"


def read_safe_mapping(path: Path, email: str) -> str | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("email") != email:
        raise ValueError(f"invalid contributor mapping record: {path}")
    login = data.get("login")
    if not isinstance(login, str) or not login:
        raise ValueError(f"invalid contributor mapping record: {path}")
    return login


def load_safe_mappings(directory: Path) -> dict[str, str]:
    safe_directory = directory / "by-hash"
    if not safe_directory.is_dir():
        return {}
    mappings = {}
    for path in sorted(safe_directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"invalid contributor mapping record: {path}")
        email = data.get("email")
        login = data.get("login")
        if not isinstance(email, str) or not isinstance(login, str) or not login:
            raise ValueError(f"invalid contributor mapping record: {path}")
        if safe_mapping_path(directory, email) != path:
            raise ValueError(f"contributor mapping filename does not match email: {path}")
        mappings[email] = login
    return mappings


def write_safe_mapping(directory: Path, email: str, login: str, comment: str = "") -> Path:
    path = safe_mapping_path(directory, email)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"email": email, "login": login}
    if comment:
        data["comment"] = comment
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path
