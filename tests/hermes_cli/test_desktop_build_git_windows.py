"""The installer stage and later desktop product build are separate processes."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _managed_npm_layout(
    tmp_path: Path, npm_version: str, node_version: str, *, arch: str = "x64", cli: bool = True,
) -> tuple[Path, Path, Path]:
    """Create PM's paired Windows node/npm layout without executing Windows shims."""
    tools = tmp_path / "tools"
    npm = tools / f"npm-{npm_version}-win32-{arch}" / "npm.cmd"
    node = tools / f"node-{node_version}-win32-{arch}" / "node.exe"
    npm.parent.mkdir(parents=True)
    node.parent.mkdir(parents=True)
    npm.touch()
    node.touch()
    npm_cli = npm.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    node_bundled_npm_cli = node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    node_bundled_npm_cli.parent.mkdir(parents=True)
    node_bundled_npm_cli.touch()
    if cli:
        npm_cli.parent.mkdir(parents=True)
        npm_cli.touch()
    return npm, node, npm_cli


def _record_pm_pair(
    tmp_path: Path, npm: Path, node: Path, *, npm_arch: str = "x64", node_arch: str = "x64",
) -> None:
    tools = tmp_path / "tools"
    (tools / "facts.json").write_text(json.dumps({
        "schema": 1,
        "packages": {
            "npm": {"entry": npm.parent.name, "version": npm.parent.name.split("-")[1], "target": f"win32-{npm_arch}"},
            "node": {"entry": node.parent.name, "version": node.parent.name.split("-")[1], "target": f"win32-{node_arch}"},
        },
    }), encoding="utf-8")


def test_managed_windows_npm_cmd_uses_pm_selected_differently_versioned_node_and_cli(tmp_path: Path) -> None:
    from hermes_cli.main_desktop import _npm_command

    npm, node, npm_cli = _managed_npm_layout(tmp_path, "12.0.2", "26.7.0")
    node_bundled_npm_cli = node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    _managed_npm_layout(tmp_path, "12.1.0", "27.0.0")
    _record_pm_pair(tmp_path, npm, node)

    assert node_bundled_npm_cli.is_file()
    assert node_bundled_npm_cli != npm_cli
    assert _npm_command(str(npm), windows=True) == [str(node), str(npm_cli)]


@pytest.mark.parametrize("missing", ["node", "cli"])
def test_managed_windows_npm_cmd_never_falls_back_to_cmd_when_its_pair_is_incomplete(
    tmp_path: Path, missing: str,
) -> None:
    from hermes_cli.main_desktop import ManagedDesktopNpmError, _npm_command

    npm, node, npm_cli = _managed_npm_layout(tmp_path, "12.0.2", "26.7.0", cli=missing != "cli")
    _record_pm_pair(tmp_path, npm, node)
    if missing == "node":
        node.unlink()

    with pytest.raises(ManagedDesktopNpmError, match="managed npm.cmd"):
        _npm_command(str(npm), windows=True)


def test_managed_windows_npm_cmd_rejects_ambiguous_unrecorded_node_candidates(tmp_path: Path) -> None:
    from hermes_cli.main_desktop import ManagedDesktopNpmError, _npm_command

    npm, _node, _cli = _managed_npm_layout(tmp_path, "12.0.2", "26.7.0")
    _managed_npm_layout(tmp_path, "12.1.0", "27.0.0")

    with pytest.raises(ManagedDesktopNpmError, match="PM-selected"):
        _npm_command(str(npm), windows=True)


def test_managed_windows_npm_cmd_rejects_pm_arch_mismatch(tmp_path: Path) -> None:
    from hermes_cli.main_desktop import ManagedDesktopNpmError, _npm_command

    npm, node, _cli = _managed_npm_layout(tmp_path, "12.0.2", "26.7.0")
    _record_pm_pair(tmp_path, npm, node, node_arch="arm64")

    with pytest.raises(ManagedDesktopNpmError, match="architecture"):
        _npm_command(str(npm), windows=True)


def test_managed_windows_npm_cmd_rejects_pm_npm_arch_mismatch(tmp_path: Path) -> None:
    from hermes_cli.main_desktop import ManagedDesktopNpmError, _npm_command

    npm, node, _cli = _managed_npm_layout(tmp_path, "12.0.2", "26.7.0")
    _record_pm_pair(tmp_path, npm, node, npm_arch="arm64")

    with pytest.raises(ManagedDesktopNpmError, match="architecture"):
        _npm_command(str(npm), windows=True)


def test_non_cmd_npm_keeps_its_normal_invocation(tmp_path: Path) -> None:
    from hermes_cli.main_desktop import _npm_command

    npm = tmp_path / "npm"
    npm.touch()

    assert _npm_command(str(npm), windows=True) == [str(npm)]


@pytest.mark.platforms("windows")
def test_packaged_desktop_build_and_builder_share_the_managed_node_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pm
    from hermes_cli import main_desktop

    npm, node, npm_cli = _managed_npm_layout(tmp_path, "12.0.2", "26.7.0")
    _record_pm_pair(tmp_path, npm, node)
    desktop = tmp_path / "apps" / "desktop"
    desktop.mkdir(parents=True)
    calls: list[list[str]] = []
    original = {"PATH": "C:\\without-git", "HERMES_HOME": str(tmp_path)}

    monkeypatch.setattr(pm, "ensure", lambda *names, base_env: SimpleNamespace(env=base_env))
    monkeypatch.setattr(
        main_desktop.subprocess, "run", lambda command, **_kwargs: calls.append(command),
    )
    monkeypatch.setattr(main_desktop, "_promote_staged_desktop_app", lambda *_args: desktop / "Hermes.exe")

    main_desktop.build_prepared_desktop(desktop, source_mode=False, npm=str(npm), env=original)

    assert [command[:4] for command in calls] == [
        [str(node), str(npm_cli), "run", "build"],
        [str(node), str(npm_cli), "run", "builder"],
    ]


@pytest.mark.platforms("windows")
def test_packaged_desktop_build_restores_pm_git_for_stamp_and_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pm
    from hermes_cli import main_desktop

    desktop = tmp_path / "apps" / "desktop"
    desktop.mkdir(parents=True)
    original = {"PATH": "C:\\without-git", "HERMES_HOME": str(tmp_path)}
    calls: list[tuple[list[str], dict[str, str]]] = []

    def installed_git(*names: str, base_env: dict[str, str]) -> dict[str, str]:
        assert names == ("git",)
        assert base_env == original
        return {**base_env, "PATH": "C:\\pm-pinned-git\\cmd;" + base_env["PATH"]}

    def run(command: list[str], *, env: dict[str, str], **_kwargs: object) -> None:
        calls.append((command, env))

    monkeypatch.setattr(pm, "ensure", lambda *names, base_env: SimpleNamespace(env=installed_git(*names, base_env=base_env)))
    monkeypatch.setattr(main_desktop.subprocess, "run", run)
    monkeypatch.setattr(main_desktop, "_promote_staged_desktop_app", lambda *_args: desktop / "Hermes.exe")
    main_desktop.build_prepared_desktop(desktop, source_mode=False, npm="C:\\node\\npm.cmd", env=original)

    assert [command[2] for command, _ in calls] == ["build", "builder"]
    assert all(env["PATH"].startswith("C:\\pm-pinned-git\\cmd;") for _, env in calls)
    assert original["PATH"] == "C:\\without-git"
