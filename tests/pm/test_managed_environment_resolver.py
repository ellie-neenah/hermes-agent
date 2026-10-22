"""Managed PM state must win over adjacent legacy checkout virtualenvs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pm import environments


def _write_managed_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    root = tmp_path / "hermes-agent"
    root.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr(environments, "dependency_home_root", lambda: home)

    generation = environments.install_state_dir(root) / "environments" / "cp314"
    python = environments.venv_python(generation)
    python.parent.mkdir(parents=True)
    python.write_text("managed cp314", encoding="utf-8")
    (generation / "pyvenv.cfg").write_text("version = 3.14.0\n", encoding="utf-8")
    environments.runtime_facts_path(root).write_text(
        json.dumps({"packages": {"venv": {"environment": str(generation)}}}), encoding="utf-8"
    )

    legacy = root / "venv"
    legacy_python = environments.venv_python(legacy)
    legacy_python.parent.mkdir(parents=True)
    legacy_python.write_text("legacy cp311", encoding="utf-8")
    (legacy / "pyvenv.cfg").write_text("version = 3.11.0\n", encoding="utf-8")
    return root, generation, legacy_python


def test_managed_resolver_uses_recorded_cp314_not_adjacent_cp311(tmp_path, monkeypatch):
    root, generation, legacy_python = _write_managed_state(tmp_path, monkeypatch)

    resolved = environments.managed_environment(root)

    assert resolved.environment == generation
    assert resolved.python == environments.venv_python(generation)
    assert resolved.python != legacy_python
    assert environments.project_python(root) == resolved.python


def test_completion_relaunch_uses_recorded_managed_python_not_legacy_checkout_venv(tmp_path, monkeypatch):
    from hermes_cli.source_completion import _bootstrap_command

    root, generation, legacy_python = _write_managed_state(tmp_path, monkeypatch)

    command = _bootstrap_command(root, ["--desktop"])

    assert command[0] == str(environments.venv_python(generation))
    assert command[0] != str(legacy_python)


def test_managed_resolver_fails_closed_when_recorded_interpreter_is_missing(tmp_path, monkeypatch):
    root, generation, _legacy_python = _write_managed_state(tmp_path, monkeypatch)
    environments.venv_python(generation).unlink()

    with pytest.raises(environments.ManagedEnvironmentError,
                       match=r"managed environment is missing or corrupt; run `hermes pm repair`"):
        environments.managed_environment(root)


def test_managed_resolver_leaves_unmanaged_checkout_unresolved(tmp_path, monkeypatch):
    root = tmp_path / "hermes-agent"
    root.mkdir()
    monkeypatch.setattr(environments, "dependency_home_root", lambda: tmp_path / "home")
    legacy = root / ".venv"
    environments.venv_python(legacy).parent.mkdir(parents=True)
    environments.venv_python(legacy).write_text("developer interpreter", encoding="utf-8")
    (legacy / "pyvenv.cfg").write_text("version = 3.11.0\n", encoding="utf-8")

    assert environments.managed_environment(root) is None
