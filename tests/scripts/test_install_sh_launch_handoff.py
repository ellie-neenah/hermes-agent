"""Post-PM installer stages use the real installation-bound publication."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from tests.installation_launcher_fixture import publish_fixture_launcher

if os.name == 'posix':
    import fcntl
    import pty
    import termios

ROOT = Path(__file__).resolve().parents[2]

def _with_controlling_terminal(tty_path: str):
    """preexec_fn: make this PTY the child's controlling terminal, so /dev/tty opens."""
    def attach() -> None:
        os.setsid()  # windows-footgun: ok (posix-only test)
        fd = os.open(tty_path, os.O_RDWR)
        try:
            # Opening a PTY slave does not always acquire it on Darwin.
            fcntl.ioctl(fd, termios.TIOCSCTTY, 0)
            probe = os.open('/dev/tty', os.O_RDONLY)
            os.close(probe)
        finally:
            os.close(fd)
    return attach


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('stage, expected', [('setup', ['setup']), ('gateway', ['gateway', 'install', '--if-missing'])])
def test_installer_post_pm_stages(tmp_path: Path, stage: str, expected: list[str]) -> None:
    install = tmp_path / 'source tree'
    calls = tmp_path / 'calls.json'
    publish_fixture_launcher(install, "import json, os, sys\nfrom pathlib import Path\ndef main():\n    Path(os.environ['CALLS']).write_text(json.dumps(sys.argv[1:])); return int(os.environ['STAGE_EXIT'])\n")
    command = ['bash', '-c', 'source "$1" --manifest >/dev/null; INSTALL_DIR="$2"; NON_INTERACTIVE=false; "stage_$3"', 'test', str(ROOT / 'scripts/install.sh'), str(install), stage]
    env = {**os.environ, 'HOME': str(tmp_path), 'HERMES_HOME': str(tmp_path / 'home'), 'HERMES_RUNTIME_DIR': str(tmp_path / 'store'), 'CALLS': str(calls)}
    # `curl | bash` and Docker builds have no terminal: the interactive stage is skipped, not failed.
    result = subprocess.run(command, cwd=tmp_path, env={**env, 'STAGE_EXIT': '9'}, capture_output=True, text=True,
                            encoding='utf-8', timeout=20, start_new_session=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not calls.exists()
    master, slave = pty.openpty()
    try:
        tty_path = os.ttyname(slave)
    finally:
        os.close(slave)
    try:
        for code in [0, 9]:
            result = subprocess.run(command, cwd=tmp_path, env={**env, 'STAGE_EXIT': str(code)}, capture_output=True,
                                    text=True, encoding='utf-8', timeout=20, stdin=subprocess.DEVNULL,
                                    preexec_fn=_with_controlling_terminal(tty_path), check=False)
            assert calls.exists(), (result.returncode, result.stdout, result.stderr)
            assert (result.returncode == 0) == (code == 0), result.stdout + result.stderr
            assert json.loads(calls.read_text()) == expected
    finally:
        os.close(master)
    assert not (install / 'venv').exists()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('stage, include_desktop, expected_desktop, build_fails', [
    ('products', False, False, False),
    ('products', True, True, False),
    ('desktop', False, True, False),
    pytest.param('products', True, True, True, id='desktop-build-failure-no-python-fallback'),
])
def test_products_and_desktop_stages_share_the_completion_tail(tmp_path: Path, stage: str,
                                                                include_desktop: bool, expected_desktop: bool,
                                                                build_fails: bool) -> None:
    """Both stages hand the checkout to hermes_cli/source_completion.py; --include-desktop
    (or the external `desktop` stage) adds --desktop. A failed Desktop build is not retried
    with a legacy Python (ellie-neenah/hermes-agent#4)."""
    install = tmp_path / 'source tree'
    calls = tmp_path / 'calls.json'
    build_error = 'error: Desktop packaging fixture failed to write the bundle'
    (install / 'hermes_cli').mkdir(parents=True)
    (install / 'hermes_cli' / 'source_completion.py').write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        "with Path(os.environ['CALLS']).open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if '--desktop' in sys.argv and os.environ.get('BUILD_ERROR'):\n"
        "    print(os.environ['BUILD_ERROR'], file=sys.stderr)\n"
        "    sys.exit(7)\n",
        encoding='utf-8',
    )
    legacy_calls = tmp_path / 'legacy-calls'
    bin_dir = tmp_path / 'bin'
    for candidate in (bin_dir / 'python', bin_dir / 'python3', install / 'venv/bin/python',
                      install / '.venv/bin/python'):
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text('#!/bin/sh\nprintf "%s\\n" "$0" >> "$LEGACY_CALLS"\nexit 0\n',
                             encoding='utf-8')
        candidate.chmod(0o755)
    flag = 'true' if include_desktop else 'false'
    command = ['bash', '-c',
               (f'source "$1" --manifest >/dev/null; INSTALL_DIR="$2"; INCLUDE_DESKTOP={flag}; '
                'bootstrap_python() { boot_py="$PYTHON_FOR_TEST"; }; "stage_$3"'),
               'test', str(ROOT / 'scripts/install.sh'), str(install), stage]
    env = {'HOME': str(tmp_path), 'HERMES_HOME': str(tmp_path / 'home'), 'TMPDIR': str(tmp_path),
           'PATH': os.pathsep.join((str(bin_dir), os.defpath)), 'CALLS': str(calls),
           'HERMES_RUNTIME_DIR': str(tmp_path / 'store'), 'PYTHON_FOR_TEST': sys.executable,
           'LEGACY_CALLS': str(legacy_calls), 'BUILD_ERROR': build_error if build_fails else ''}
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True,
                            encoding='utf-8', timeout=20, check=False)
    assert not legacy_calls.exists(), f'legacy Python was invoked:\n{legacy_calls.read_text(encoding="utf-8")}'
    assert result.returncode == int(build_fails), result.stdout + result.stderr
    expected_args = ['--source', str(install)] + (['--desktop'] if expected_desktop else [])
    assert [json.loads(line) for line in calls.read_text(encoding='utf-8').splitlines()] == [expected_args]
    assert ('app products and hermes command ready' in result.stdout) is not build_fails
    if build_fails:
        assert build_error in result.stderr


@pytest.mark.platforms("posix")
def test_desktop_flag_does_not_add_a_stage(tmp_path):
    """--include-desktop selects the desktop product inside `products`; the manifest is one ladder."""
    def manifest(*flags):
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/install.sh"), "--manifest", *flags],
            cwd=tmp_path, capture_output=True, text=True, timeout=30, check=True,
        )
        return [row["name"] for row in json.loads(result.stdout)["stages"]]

    plain = manifest()
    assert "desktop" not in plain
    assert "products" in plain
    assert plain[-1] == "complete"
    assert manifest("--include-desktop") == plain
