"""Desktop launch prepares once, then stages and publishes the local pack."""
from argparse import Namespace
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import main, main_desktop
from tests.hermes_cli.test_source_build import source_checkout, source_products, _events


@pytest.fixture
def desktop_source(source_products, monkeypatch):
    root, acquired = source_products
    monkeypatch.setattr(main, "PROJECT_ROOT", root)
    monkeypatch.setattr(main_desktop, "_desktop_launch_env", lambda args: ({}, []))
    monkeypatch.setattr(main_desktop, "_register_linux_desktop_entry", lambda **kwargs: None)
    return root, acquired


@pytest.mark.platforms("linux")
def test_desktop_build_only_prepares_once_and_keeps_fresh_launch_fast(desktop_source):
    root, acquired = desktop_source
    main_desktop.cmd_gui(Namespace(build_only=True))
    app = root / "apps/desktop/release/linux-unpacked/hermes"
    assert app.read_text() == "desktop"
    assert [event["step"] for event in _events(root)] == ["deps", "icons", "desktop"]
    assert acquired == ["pnpm"]
    assert (root / "ui-tui/node_modules").exists()
    assert (root / "web/node_modules").exists()
    assert (root / "apps/desktop/node_modules").exists()
    assert not (root / "unrelated/node_modules").exists()
    main_desktop.cmd_gui(Namespace(build_only=True))
    assert acquired == ["pnpm"]
    assert len(_events(root)) == 3


@pytest.mark.platforms("linux")
def test_failed_pack_exits_without_launching_or_replacing_the_app(desktop_source):
    root, acquired = desktop_source
    app = root / "apps/desktop/release/linux-unpacked/hermes"
    app.parent.mkdir(parents=True)
    app.write_text("previous app")
    (root / "fail-desktop").touch()
    with pytest.raises(SystemExit) as error:
        main_desktop.cmd_gui(Namespace(build_only=True))
    assert error.value.code != 0
    assert app.read_text() == "previous app"
    assert acquired == ["pnpm"]
    assert [event["step"] for event in _events(root)] == ["deps", "icons", "desktop"]
    assert not list((root / "apps/desktop").glob(".staging-*"))


@pytest.mark.platforms("linux")
def test_skip_build_source_checks_artifacts_without_provisioning(desktop_source):
    root, acquired = desktop_source
    with pytest.raises(SystemExit):
        main_desktop.cmd_gui(Namespace(source=True, skip_build=True, build_only=True))
    assert acquired == []
    dist = root / "apps/desktop/dist"
    dist.mkdir()
    (dist / "index.html").write_text("prepared renderer")
    electron = root / "apps/desktop/node_modules/electron"
    electron.mkdir(parents=True)
    (electron / "package.json").write_text('{}')
    main_desktop.cmd_gui(Namespace(source=True, skip_build=True, build_only=True))
    assert acquired == []


@pytest.mark.platforms("windows")
def test_windows_desktop_build_avoids_foreign_npm_cmd(tmp_path, monkeypatch):
    """Exercise the build drivers; substitute process execution, not a live policy denial."""
    import pm
    from hermes_cli import desktop_build_lock, linux_desktop_entry, source_build, steward, update_stage

    node = shutil.which("node")
    assert node, "This test requires Node, but not pnpm or Electron"
    root = tmp_path / "source with spaces"
    desktop = root / "apps" / "desktop"
    desktop.mkdir(parents=True)
    tools = tmp_path / "managed tools"
    tools.mkdir()
    pnpm = tools / "pnpm.exe"
    pnpm.touch()
    npm = tools / "npm.CMD"
    npm.write_text("@exit /b 1\n", encoding="utf-8")
    foreign = tmp_path / "foreign" / "npm-cli.js"
    foreign.parent.mkdir()
    foreign.touch()
    (tools / "package.json").write_text('{"version":"12.0.0"}', encoding="utf-8")
    (root / "pm").mkdir()
    (root / "pm" / "lock.json").write_text('{"packages":{"pnpm":{"version":"12.0.0"}}}', encoding="utf-8")
    (root / "package.json").write_text("{}", encoding="utf-8")
    for workspace in ("ui-tui", "web", "apps/desktop"):
        directory = root / workspace
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "package.json").write_text("{}", encoding="utf-8")
    (root / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\nimporters:\n  .: {}\n  ui-tui: {}\n  web: {}\n  apps/desktop: {}\n",
        encoding="utf-8",
    )
    repository = Path(__file__).resolve().parents[2]
    scripts = root / "scripts" / "build"
    scripts.mkdir(parents=True)
    for name in ("node-deps.mjs", "node-deps-lock.mjs", "node-deps-heartbeat.mjs",
                 "pnpm-lock.mjs", "electron-runtime.mjs"):
        shutil.copy2(repository / "scripts" / "build" / name, scripts / name)

    # Intercept the JS child-process boundary: record the command handed to the
    # lock relay and simulate only its installed-lock output, without running pnpm.
    preload = tmp_path / "intercept.mjs"
    events = tmp_path / "commands.json"
    preload.write_text(
        "import child from 'node:child_process';\n"
        "import { syncBuiltinESMExports } from 'node:module';\n"
        "import { copyFileSync, mkdirSync, writeFileSync } from 'node:fs';\n"
        "import { join } from 'node:path';\n"
        "child.spawnSync = (file, args, opts) => {\n"
        "  writeFileSync(process.env.TEST_COMMANDS, JSON.stringify({file, args}));\n"
        "  mkdirSync(join(opts.cwd, 'node_modules', '.pnpm'), {recursive: true});\n"
        "  copyFileSync(join(opts.cwd, 'pnpm-lock.yaml'), join(opts.cwd, 'node_modules', '.pnpm', 'lock.yaml'));\n"
        "  return {status: 0};\n"
        "};\n"
        "child.execFileSync = () => { throw new Error('unexpected child process'); };\n"
        "syncBuiltinESMExports();\n",
        encoding="utf-8",
    )
    env = {**{key: value for key, value in os.environ.items()
              if key.upper() not in {"PATH", "NPM_EXECPATH", "NODE_OPTIONS"}},
           "PATH": os.pathsep.join((str(tools), str(Path(node).parent))),
           "npm_execpath": str(foreign), "TEST_COMMANDS": str(events)}
    calls = []

    def run(root_arg, command, label, **kwargs):
        calls.append((command, kwargs["cwd"], kwargs["env"]))
        if label == "Preparing Node dependencies":
            assert Path(command[0]) == Path(node)
            assert Path(command[1]) == scripts / "node-deps.mjs"
            subprocess.run([command[0], "--import", preload.as_uri(), *command[1:]],
                           cwd=kwargs["cwd"], env=kwargs["env"], check=True)

    monkeypatch.setattr(main, "PROJECT_ROOT", root)
    monkeypatch.setattr(main_desktop, "_desktop_launch_env", lambda args: (dict(env), []))
    monkeypatch.setattr(steward, "is_bundled_payload", lambda _: False)
    monkeypatch.setattr(source_build, "source_build_env", lambda *_args, **_kwargs: dict(env))
    monkeypatch.setattr(source_build, "run_in_custody", run)
    monkeypatch.setattr(pm, "ensure", lambda name, *, base_env: SimpleNamespace(env=base_env))
    monkeypatch.setattr(desktop_build_lock.DesktopBuildLock, "acquire", lambda self: True)
    monkeypatch.setattr(desktop_build_lock.DesktopBuildLock, "release", lambda self: None)
    monkeypatch.setattr(main_desktop, "_desktop_ancestor_in", lambda _: None)
    monkeypatch.setattr(main_desktop, "_stop_desktop_processes_locking_build", lambda _: [])
    monkeypatch.setattr(main_desktop, "_promote_staged_desktop_app", lambda *_: desktop / "Hermes.exe")
    monkeypatch.setattr(main_desktop, "_register_linux_desktop_entry", lambda **_: None)
    monkeypatch.setattr(linux_desktop_entry, "launched_from_shell", lambda: False)
    monkeypatch.setattr(update_stage, "publish_stage", lambda _: None)

    main_desktop.cmd_gui(Namespace(build_only=True))
    assert len(calls) == 3
    assert Path(calls[0][0][0]) == Path(node)
    assert Path(calls[0][0][1]) == scripts / "node-deps.mjs"
    assert [call[0][1:3] for call in calls[1:]] == [["run", "build"], ["run", "builder"]]
    assert all(Path(call[0][0]) == pnpm and call[1] == desktop for call in calls[1:])
    assert all(call[2]["npm_execpath"] == str(foreign) for call in calls)
    relay = json.loads(events.read_text(encoding="utf-8"))
    assert Path(relay["file"]) == Path(node)
    assert Path(relay["args"][0]) == scripts / "node-deps-relay.mjs"
    assert Path(relay["args"][4]) == pnpm
    assert relay["args"][5:9] == ["install", "--frozen-lockfile", "--trust-lockfile", "--prod=false"]
    assert relay["args"][9:] == ["--filter", "{./apps/desktop}...", "--filter", "{./ui-tui}...",
                                 "--filter", "{./web}..."]

    pnpm.unlink()
    events.unlink()
    result = subprocess.run(
        [node, "--import", preload.as_uri(), str(scripts / "node-deps.mjs"), "--source", str(root),
         "--workspace", "apps/desktop"], cwd=root, env={**env, "PATH": str(tools)},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "pnpm is required on PATH" in result.stderr
    assert not events.exists()
