"""Exit-status contracts for read-only update plans and ordinary update dispatch."""

from types import SimpleNamespace

import pytest

from hermes_cli import main
from hermes_cli.source_build import ProductNpmPreflight


@pytest.fixture
def update_cli(monkeypatch, tmp_path):
    from hermes_cli import config, source_build, update_inventory, update_owning_install, venv_sync

    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    for name in ("_set_process_title", "_warn_if_unsupervised_pid1", "_advertise_agent_env",
                 "_cleanup_quarantined_exes", "_sweep_stale_bytecode_if_checkout_changed",
                 "_recover_update_debts_on_startup"):
        monkeypatch.setattr(main, name, lambda: None)
    monkeypatch.setattr(main, "_prepare_agent_startup", lambda _args: None)
    monkeypatch.setattr(config, "get_container_exec_info", lambda: None)
    monkeypatch.setattr(config, "is_managed", lambda: False)
    monkeypatch.setattr(venv_sync, "check_runtime", lambda _root: None)
    monkeypatch.setattr(update_owning_install, "retarget_to_owning_install", lambda _root: None)
    monkeypatch.setattr(update_inventory, "collect_runtime_inventory", lambda: None)
    monkeypatch.setattr(update_inventory, "print_update_plan", lambda _plan: print("fixture update plan"))
    monkeypatch.setattr(source_build, "is_windows_source_desktop_update", lambda _root: True)

    def run(*args):
        monkeypatch.setattr(main.sys, "argv", ["hermes", "update", *args])
        try:
            result = main.main()
        except SystemExit as exc:
            return exc.code
        return 0 if result is None else result

    return run


@pytest.mark.parametrize(
    ("policy_denied", "detail", "message"),
    [(True, "blocked by group policy", "Windows Group Policy denied"),
     (False, "tool unavailable", "packaging vector is unavailable")],
)
def test_plan_no_go_exits_nonzero_without_starting_update(update_cli, monkeypatch, capsys,
                                                           policy_denied, detail, message):
    from hermes_cli import source_build

    monkeypatch.setattr(source_build, "product_npm_preflight", lambda _root:
                        ProductNpmPreflight(None, None, None, False, policy_denied, detail))
    monkeypatch.setattr(main, "_install_hangup_protection", lambda **_kwargs:
                        pytest.fail("read-only plan started update"))

    assert update_cli("--plan") != 0
    out = capsys.readouterr().out
    assert "fixture update plan" in out
    assert "NO-GO" in out and message in out and "No update work was started" in out


def test_ready_plan_exits_zero_without_starting_update(update_cli, monkeypatch, capsys):
    from hermes_cli import source_build

    monkeypatch.setattr(source_build, "product_npm_preflight", lambda _root:
                        ProductNpmPreflight(None, None, None, True, False, "fixture-version"))
    monkeypatch.setattr(main, "_install_hangup_protection", lambda **_kwargs:
                        pytest.fail("read-only plan started update"))

    assert update_cli("--plan") == 0
    out = capsys.readouterr().out
    assert "fixture update plan" in out and "is ready" in out
    assert "NO-GO" not in out


def test_ordinary_update_dispatch_still_runs_and_exits_zero(update_cli, monkeypatch):
    from hermes_cli import update_cmd, update_contract, update_lock

    calls = []
    monkeypatch.setattr(update_contract, "evaluate_update_admission", lambda _root: None)
    monkeypatch.setattr(main, "_install_hangup_protection", lambda **_kwargs: "fixture io")
    monkeypatch.setattr(main, "_finalize_update_output", lambda state: calls.append(("output", state)))
    monkeypatch.setattr(main, "_finalize_update_receipt", lambda code, _reason: calls.append(("receipt", code)))
    monkeypatch.setattr(update_lock, "UpdateLock", lambda **_kwargs: SimpleNamespace(
        acquire=lambda: True, release=lambda: calls.append(("release",))))
    monkeypatch.setattr(update_cmd, "_cmd_update_impl", lambda args, gateway_mode:
                        calls.append(("update", args.plan, gateway_mode)))

    assert update_cli() == 0
    assert calls == [("update", False, False), ("receipt", 0), ("release",), ("output", "fixture io")]


def test_ordinary_update_failure_still_propagates(update_cli, monkeypatch):
    from hermes_cli import update_cmd, update_contract, update_lock

    calls = []
    monkeypatch.setattr(update_contract, "evaluate_update_admission", lambda _root: None)
    monkeypatch.setattr(main, "_install_hangup_protection", lambda **_kwargs: "fixture io")
    monkeypatch.setattr(main, "_finalize_update_output", lambda state: calls.append(("output", state)))
    monkeypatch.setattr(main, "_finalize_update_receipt", lambda code, _reason: calls.append(("receipt", code)))
    monkeypatch.setattr(update_lock, "UpdateLock", lambda **_kwargs: SimpleNamespace(
        acquire=lambda: True, release=lambda: calls.append(("release",))))

    def failed_update(_args, gateway_mode):
        assert not gateway_mode
        raise SystemExit(1)

    monkeypatch.setattr(update_cmd, "_cmd_update_impl", failed_update)
    assert update_cli() == 1
    assert calls == [("receipt", 1), ("release",), ("output", "fixture io")]
