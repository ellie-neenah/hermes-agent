"""Committed dependency selection must not adopt a neighboring legacy venv."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pm import environments


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "checkout"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    state = environments.install_state_dir(root)
    state.mkdir(parents=True)
    return root, state


def _legacy_venv(root: Path) -> tuple[Path, Path]:
    legacy = root / "venv"
    site = environments.site_packages(legacy)
    site.mkdir(parents=True)
    (legacy / "pyvenv.cfg").write_text("version = 99.0-test-only\n", encoding="ascii")
    return legacy, site


def _record_generation(root: Path, state: Path, generation: Path) -> None:
    (state / "environments").mkdir(exist_ok=True)
    environments.runtime_facts_path(root).write_text(
        json.dumps({"packages": {"venv": {"environment": str(generation)}}}),
        encoding="utf-8",
    )


def test_missing_managed_generation_never_falls_back_to_legacy_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state = _install(tmp_path, monkeypatch)
    _legacy, legacy_site = _legacy_venv(root)
    missing = state / "environments" / "missing-test-generation"
    _record_generation(root, state, missing)
    before = sys.path[:]

    with pytest.raises(RuntimeError, match="missing or outside"):
        environments.selected_venv(root)
    with pytest.raises(RuntimeError, match="missing or outside"):
        environments.activate_dependencies(root)

    assert sys.path == before
    assert str(legacy_site) not in sys.path


def test_managed_generation_without_site_packages_never_uses_legacy_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state = _install(tmp_path, monkeypatch)
    _legacy, legacy_site = _legacy_venv(root)
    broken = state / "environments" / "broken-test-generation"
    broken.mkdir(parents=True)
    (broken / "pyvenv.cfg").write_text("version = 99.0-test-only\n", encoding="ascii")
    _record_generation(root, state, broken)
    before = sys.path[:]

    assert environments.selected_venv(root) == broken
    with pytest.raises(RuntimeError, match="has no site-packages"):
        environments.activate_dependencies(root)

    assert sys.path == before
    assert str(legacy_site) not in sys.path


def test_unmanaged_checkout_keeps_legacy_selection_and_current_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    legacy, legacy_site = _legacy_venv(root)
    before = sys.path[:]

    assert environments.selected_venv(root) == legacy
    environments.activate_dependencies(root)

    assert sys.path == before
    assert str(legacy_site) not in sys.path
