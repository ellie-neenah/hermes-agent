"""Runtime coverage for provider-confirmed context-pressure notices."""
from __future__ import annotations

from types import SimpleNamespace

from agent import turn_usage
from agent.usage_pricing import CanonicalUsage


class _Compressor:
    context_length = 100_000
    threshold_tokens = 90_000
    _verify_compaction_cleared_threshold = False
    _context_probed = False

    def __init__(self):
        self.usage_updates = []

    def update_from_response(self, usage):
        self.usage_updates.append(usage)


def _agent(statuses):
    """The live usage recorder's required runtime surface, without bootstrap I/O."""
    return SimpleNamespace(
        context_compressor=_Compressor(),
        session_api_calls=0,
        model="anthropic/claude-fable-5.1",
        provider="nous",
        api_mode="chat_completions",
        base_url="https://inference-api.nousresearch.com/v1",
        api_key="k",
        client=None,
        session_id="context-pressure-runtime",
        session_prompt_tokens=0,
        session_completion_tokens=0,
        session_total_tokens=0,
        session_input_tokens=0,
        session_output_tokens=0,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_reasoning_tokens=0,
        session_estimated_cost_usd=0.0,
        session_cost_status=None,
        session_cost_source=None,
        _session_db=None,
        verbose_logging=False,
        quiet_mode=True,
        log_prefix="",
        _emit_status_kind=lambda kind, text, *, origin: statuses.append((kind, text, origin)),
    )


def _response(prompt_tokens: int):
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=5,
            total_tokens=prompt_tokens + 5,
            prompt_tokens_details=None,
            completion_tokens_details=None,
        ),
        id="resp-context-pressure",
        model="anthropic/claude-fable-5.1",
    )


def _record(agent, prompt_tokens: int, api_call_count: int):
    return turn_usage.record_response_usage(
        agent,
        _response(prompt_tokens),
        messages=[{"role": "user", "content": "hi"}],
        api_call_count=api_call_count,
        api_duration=0.1,
        compression_attempts=0,
        max_compression_attempts=3,
    )


def test_record_response_usage_emits_at_sixty_dedupes_and_rearms_after_reset(monkeypatch):
    statuses = []
    agent = _agent(statuses)
    monkeypatch.setattr(turn_usage, "_loop_mod", lambda: SimpleNamespace(_should_rearm_compression_budget=lambda *a, **k: False))

    _record(agent, 59_999, 1)  # Ordinary provider usage must not NameError or notify below 60%.
    _record(agent, 60_000, 2)
    _record(agent, 80_000, 3)
    assert statuses == [("context_pressure", "60 provider-confirmed", "maybe_emit_context_pressure")]

    # /new and /reset rearm session-local latches; model the reset boundary directly.
    agent._context_pressure_notified = False
    _record(agent, 61_000, 4)

    assert statuses == [
        ("context_pressure", "60 provider-confirmed", "maybe_emit_context_pressure"),
        ("context_pressure", "61 provider-confirmed", "maybe_emit_context_pressure"),
    ]


class _MoAClient:
    last_aggregator_slot = {"provider": "nous", "model": "anthropic/claude-fable-5.1"}

    def consume_reference_usage(self):
        # Large advisor fan-out belongs in aggregate accounting, not the acting context window.
        return CanonicalUsage(input_tokens=80_000, output_tokens=1), None


def test_record_response_usage_uses_only_moa_aggregator_usage_for_context_pressure(monkeypatch):
    statuses = []
    agent = _agent(statuses)
    agent.client = _MoAClient()
    monkeypatch.setattr(turn_usage, "_loop_mod", lambda: SimpleNamespace(_should_rearm_compression_budget=lambda *a, **k: False))

    _record(agent, 59_999, 1)
    assert agent.context_compressor.usage_updates[-1]["prompt_tokens"] == 59_999
    assert agent.session_prompt_tokens == 139_999
    assert statuses == []

    _record(agent, 60_000, 2)
    assert statuses == [("context_pressure", "60 provider-confirmed", "maybe_emit_context_pressure")]
