"""Desktop product builds use the npm entry recorded by PM."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pm
import pytest


def test_product_npm_uses_the_recorded_npm_not_the_node_bundle(tmp_path, monkeypatch):
    from hermes_cli.source_build import product_npm_command

    root = tmp_path / "source"
    resolver = root / "scripts/build/node-deps.mjs"
    node = tmp_path / "node/node.exe"
    bundled = node.parent / "node_modules/npm/bin/npm-cli.js"
    npm_root = tmp_path / "npm"
    pinned = npm_root / "node_modules/npm/bin/npm-cli.js"
    for path in (resolver, node, bundled, pinned):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    records = {
        "node": SimpleNamespace(path=node.parent, binary=node),
        "npm": SimpleNamespace(path=npm_root, binary=npm_root / "npm.cmd"),
    }
    monkeypatch.setattr(pm, "installed_package", lambda name: records[name])
    env = {}

    assert product_npm_command(root, env=env) == [str(node), str(resolver), "--npm"]
    assert env["npm_execpath"] == str(pinned)


@pytest.mark.parametrize("recorded", [False, True])
def test_product_npm_refuses_missing_recorded_npm_even_when_node_bundle_exists(tmp_path, monkeypatch, recorded):
    from hermes_cli.source_build import product_npm_command

    root = tmp_path / "source"
    resolver = root / "scripts/build/node-deps.mjs"
    node = tmp_path / "node/node.exe"
    bundled = node.parent / "node_modules/npm/bin/npm-cli.js"
    for path in (resolver, node, bundled):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    records = {"node": SimpleNamespace(binary=node),
               "npm": SimpleNamespace(path=tmp_path / "missing") if recorded else None}
    monkeypatch.setattr(pm, "installed_package", lambda name: records[name])

    with pytest.raises(RuntimeError, match="Hermes-managed Node/npm toolchain"):
        product_npm_command(root, env={})


def test_recorded_npm_preparation_receipt_is_reused_and_version_bound(tmp_path, monkeypatch):
    """The actual resolver reuses only a receipt for the selected npm version."""
    from hermes_cli.source_build import product_npm_command

    node = shutil.which("node")
    assert node
    root = tmp_path / "source"
    scripts = root / "scripts" / "build"
    scripts.mkdir(parents=True)
    repository = Path(__file__).resolve().parents[2]
    for name in ("node-deps.mjs", "electron-runtime.mjs"):
        shutil.copy2(repository / "scripts" / "build" / name, scripts / name)
    (root / "web").mkdir()
    (root / "package.json").write_text(json.dumps({"name": "fixture", "version": "1.0.0"}))
    (root / "web" / "package.json").write_text(json.dumps({"name": "web", "version": "1.0.0"}))
    (root / "package-lock.json").write_text(json.dumps({"packages": {
        "": {}, "node_modules/web": {"link": True, "resolved": "web"}, "web": {"name": "web"},
    }}))
    npm_root = tmp_path / "recorded"
    npm = npm_root / "node_modules" / "npm"
    (npm / "bin").mkdir(parents=True)
    semver = npm / "node_modules" / "semver"
    semver.mkdir(parents=True)
    (semver / "index.js").write_text("exports.satisfies = () => true;\n")
    package = npm / "package.json"
    package.write_text(json.dumps({"version": "1.0.0"}))
    cli = npm / "bin" / "npm-cli.js"
    cli.write_text(
        "const fs = require('node:fs');\n"
        "fs.mkdirSync('node_modules', {recursive: true});\n"
        "fs.writeFileSync('node_modules/.package-lock.json', JSON.stringify({packages:{}}));\n"
        "fs.appendFileSync('install-count', '1');\n"
    )
    monkeypatch.setattr(pm, "installed_package", lambda name: {
        "node": SimpleNamespace(binary=node), "npm": SimpleNamespace(path=npm_root),
    }[name])
    env = {key: value for key, value in os.environ.items()
           if not key.lower().startswith("npm_config_") and key.lower() != "npm_execpath"}
    env["npm_execpath"] = str(tmp_path / "foreign" / "npm-cli.js")
    command = product_npm_command(root, env=env)
    assert env["npm_execpath"] == str(cli)
    preparation = [command[0], command[1], "--source", str(root), "--workspace", "web", "--reuse"]
    first = subprocess.run(preparation, cwd=root, env=env, capture_output=True, text=True, check=True)
    assert "installing workspace dependencies" in first.stdout
    second = subprocess.run([*preparation, "--no-install"], cwd=root, env=env,
                            capture_output=True, text=True, check=True)
    assert "reusing completed install" in second.stdout
    assert (root / "install-count").read_text() == "1"
    package.write_text(json.dumps({"version": "2.0.0"}))
    stale = subprocess.run([*preparation, "--no-install"], cwd=root, env=env, capture_output=True, text=True)
    assert stale.returncode != 0
    assert "stale or missing" in stale.stderr
    assert (root / "install-count").read_text() == "1"
