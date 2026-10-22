"""Read-only Windows source-update plan checks for the Desktop packaging vector."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


def _source_checkout(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "hermes-agent"
    resolver = root / "scripts" / "build" / "node-deps.mjs"
    node = tmp_path / "managed" / "node.exe"
    npm_cli = node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    (root / ".git").mkdir(parents=True)
    (root / "apps" / "desktop").mkdir(parents=True)
    resolver.parent.mkdir(parents=True)
    node.parent.mkdir(parents=True)
    npm_cli.parent.mkdir(parents=True)
    resolver.write_text("// resolver", encoding="utf-8")
    (root / "apps" / "desktop" / "package.json").write_text("{}", encoding="utf-8")
    node.write_bytes(b"managed node")
    npm_cli.write_text("// paired npm CLI", encoding="utf-8")
    return root, node, npm_cli


def test_windows_source_plan_reports_policy_no_go_before_any_update_mutation(tmp_path, monkeypatch, capsys):
    import pm
    from hermes_cli import main, source_build, update_owning_install

    root, node, npm_cli = _source_checkout(tmp_path)
    monkeypatch.setattr(update_owning_install, "retarget_to_owning_install", lambda _root: None)
    monkeypatch.setattr(main, "PROJECT_ROOT", root)
    monkeypatch.setattr(pm, "installed_package", lambda name, **_kwargs: (
        SimpleNamespace(binary=node) if name == "node" else None))
    monkeypatch.setattr(source_build.sys, "platform", "win32")
    monkeypatch.setattr(main.sys, "platform", "win32")
    monkeypatch.setattr(main, "_install_hangup_protection", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("update started")))

    mutations = []
    monkeypatch.setattr(main, "_finalize_update_receipt", lambda *_args: mutations.append("receipt"))
    monkeypatch.setattr(main, "_finalize_update_output", lambda *_args: mutations.append("output"))
    monkeypatch.setattr(main, "_resolve_update_branch", lambda _args: "main")
    monkeypatch.setattr(main, "_update_preflight_handled", main._update_preflight_handled)
    monkeypatch.setattr(source_build.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        OSError(1260, "This program is blocked by group policy")))

    assert main.cmd_update(SimpleNamespace(plan=True, check=False, list_venv_holders=False)) is None

    out = capsys.readouterr().out
    assert str(node) in out and str(npm_cli) in out
    assert "NO-GO" in out and "Contact IT" in out
    assert mutations == []


def test_windows_source_plan_reports_direct_resolver_ready_without_npm_cmd(tmp_path, monkeypatch, capsys):
    import pm
    from hermes_cli import main, source_build

    root, node, npm_cli = _source_checkout(tmp_path)
    npm_cmd = tmp_path / "blocked" / "npm.CMD"
    npm_cmd.parent.mkdir()
    npm_cmd.write_text("@echo blocked", encoding="utf-8")
    monkeypatch.setattr(main, "PROJECT_ROOT", root)
    monkeypatch.setattr(pm, "installed_package", lambda name, **_kwargs: (
        SimpleNamespace(binary=node) if name == "node" else None))
    monkeypatch.setattr(source_build.sys, "platform", "win32")
    monkeypatch.setattr(main.sys, "platform", "win32")
    monkeypatch.setenv("PATH", str(npm_cmd.parent))

    calls = []
    def probe(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="10.0.0\n", stderr="")
    monkeypatch.setattr(source_build.subprocess, "run", probe)

    assert main._update_preflight_handled(SimpleNamespace(plan=True, check=False, list_venv_holders=False))

    out = capsys.readouterr().out
    assert str(node) in out and str(npm_cli) in out
    assert "ready" in out.lower()
    direct_calls = [command for command, _kwargs in calls if command[0] == str(node)]
    assert direct_calls == [[str(node), str(root / "scripts/build/node-deps.mjs"), "--npm", "--version"]]
    assert all(str(npm_cmd).lower() not in " ".join(map(str, command)).lower() for command in direct_calls)


def test_non_windows_plan_does_not_probe_the_desktop_packaging_vector(tmp_path, monkeypatch):
    import pm
    from hermes_cli import main, source_build

    root, node, _npm_cli = _source_checkout(tmp_path)
    monkeypatch.setattr(main, "PROJECT_ROOT", root)
    monkeypatch.setattr(pm, "installed_package", lambda name, **_kwargs: (
        SimpleNamespace(binary=node) if name == "node" else None))
    monkeypatch.setattr(main.sys, "platform", "linux")
    monkeypatch.setattr(source_build.sys, "platform", "linux")
    monkeypatch.setattr(source_build.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("probe")))

    assert main._update_preflight_handled(SimpleNamespace(plan=True, check=False, list_venv_holders=False))
