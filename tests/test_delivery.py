"""Delivery contracts: preview never counts as a completed production run.

Every data source, AI call and Telegram operation is mocked. Fixtures are
synthetic and safe to include in the public repository.
"""

from importlib import import_module
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock

import pytest


@pytest.fixture(params=["daily_adjust", "weekly_review", "training_advisor"])
def pipeline(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A complete mocked pipeline, with no filesystem or service outputs."""
    module = import_module("scripts." + request.param)
    ledger = MagicMock()
    advice = MagicMock()
    generate = MagicMock(return_value="Synthetic generated message")
    send = MagicMock(return_value=True)
    alert = MagicMock()
    monkeypatch.setattr(module, "append_run", ledger)
    monkeypatch.setattr("scripts.telegram_bot.send_text", send)
    monkeypatch.setattr("scripts.telegram_bot.safe_send_alert", alert)

    if request.param == "daily_adjust":
        for name, value in {
            "_fetch_today_garmin_coach": None,
            "_fetch_latest_metrics": {},
            "_fetch_recent_rpe_niggle": [],
            "_fetch_recent_health_records": [],
            "_evaluate_rule_verdict": None,
            "_is_glp1_injection_day": False,
            "_is_long_run_today": False,
            "_tomorrow_long_run_preview_line": "",
            "_yesterday_task_summary": "Synthetic status",
            "build_sleep_section": "Synthetic sleep summary",
        }.items():
            monkeypatch.setattr(module, name, MagicMock(return_value=value))
        monkeypatch.setattr(module, "_generate_daily_adjustment", generate)
        monkeypatch.setattr(module, "_log_advice", advice)
        run = module.run_daily_adjust
    elif request.param == "weekly_review":
        for name, value in {
            "_fetch_active_krs": [],
            "_build_week_summary": {},
            "_compute_week_adherence": None,
            "_compute_week_sleep_trend": None,
        }.items():
            monkeypatch.setattr(module, name, MagicMock(return_value=value))
        monkeypatch.setattr(module, "_generate_weekly_review", generate)
        run = module.run_weekly_review
    else:
        for key, value in {
            "NOTION_API_KEY": "fake-key",
            "HEALTH_DB_ID": "fake-health-db",
            "ACTIVITY_DB_ID": "",
            "NUTRITION_DB_ID": "",
            "GARMIN_COACH_PLAN_ID": "",
        }.items():
            monkeypatch.setenv(key, value)
        for name, value in {
            "get_notion_client": MagicMock(),
            "_fetch_garmin_coach_context": None,
            "_load_manual_coach_week": None,
            "fetch_health_history": [],
            "build_weekly_context": MagicMock(),
            "_load_athlete_profile": MagicMock(),
            "_build_fallback_plan": "Synthetic fallback plan",
        }.items():
            monkeypatch.setattr(module, name, MagicMock(return_value=value))
        monkeypatch.setattr(module, "_generate_weekly_plan", generate)

        def run(dry_run: bool = False) -> Optional[str]:
            return module.run_weekly_advisor(dry_run=dry_run, output_telegram=True)

    return SimpleNamespace(
        name=request.param, module=module, run=run, ledger=ledger,
        advice=advice, generate=generate, send=send, alert=alert,
    )


def test_confirmed_delivery_records_success(pipeline: SimpleNamespace) -> None:
    result = pipeline.run()

    assert result is not None
    pipeline.send.assert_called_once_with(result)
    pipeline.ledger.assert_called_once_with(
        pipeline.name + ".py", ok=True, wrote_notion=False,
    )
    if pipeline.name == "daily_adjust":
        pipeline.advice.assert_called_once()


@pytest.mark.parametrize("send_result", [False, None])
def test_unconfirmed_delivery_records_failure(pipeline: SimpleNamespace, send_result: Optional[bool]) -> None:
    pipeline.send.return_value = send_result

    assert pipeline.run() is None

    pipeline.ledger.assert_called_once_with(
        pipeline.name + ".py", ok=False, wrote_notion=False,
    )
    pipeline.advice.assert_not_called()


def test_delivery_exception_records_failure_without_advice(pipeline: SimpleNamespace) -> None:
    pipeline.send.side_effect = RuntimeError("synthetic transport failure")

    assert pipeline.run() is None

    pipeline.ledger.assert_called_once_with(
        pipeline.name + ".py", ok=False, wrote_notion=False,
    )
    pipeline.advice.assert_not_called()


@pytest.mark.parametrize("generated", ["Synthetic preview", None])
def test_dry_run_has_no_production_outputs_even_on_generation_failure(
    pipeline: SimpleNamespace, generated: Optional[str],
) -> None:
    pipeline.generate.return_value = generated

    result = pipeline.run(dry_run=True)

    if generated is not None or pipeline.name == "training_advisor":
        assert result is not None
    else:
        assert result is None
    pipeline.ledger.assert_not_called()
    pipeline.advice.assert_not_called()
    pipeline.send.assert_not_called()
    pipeline.alert.assert_not_called()


def test_production_generation_failure_records_failure_or_delivered_fallback(pipeline: SimpleNamespace) -> None:
    pipeline.generate.return_value = None

    result = pipeline.run()

    fallback_delivered = pipeline.name == "training_advisor"
    assert (result is not None) == fallback_delivered
    pipeline.ledger.assert_called_once_with(
        pipeline.name + ".py", ok=fallback_delivered, wrote_notion=False,
    )
    if fallback_delivered:
        pipeline.alert.assert_called_once()
        pipeline.send.assert_called_once()
    else:
        pipeline.send.assert_not_called()


@pytest.mark.parametrize("missing_key", ["NOTION_API_KEY", "HEALTH_DB_ID"])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("output_telegram", [False, True])
def test_training_missing_settings_does_not_pollute_preview_ledger(
    monkeypatch: pytest.MonkeyPatch, missing_key: str, dry_run: bool, output_telegram: bool,
) -> None:
    from scripts import training_advisor

    ledger = MagicMock()
    monkeypatch.setenv("NOTION_API_KEY", "fake-key")
    monkeypatch.setenv("HEALTH_DB_ID", "fake-health-db")
    monkeypatch.setenv(missing_key, "")
    monkeypatch.setattr(training_advisor, "append_run", ledger)

    assert training_advisor.run_weekly_advisor(
        dry_run=dry_run, output_telegram=output_telegram,
    ) is None

    if dry_run or not output_telegram:
        ledger.assert_not_called()
    else:
        ledger.assert_called_once_with("training_advisor.py", ok=False, wrote_notion=False)


@pytest.mark.parametrize("send_result", [True, False, None])
def test_delivery_adapter_requires_explicit_confirmation(
    monkeypatch: pytest.MonkeyPatch, send_result: Optional[bool],
) -> None:
    from utils.delivery import send_telegram_text

    send = MagicMock(return_value=send_result)
    monkeypatch.setattr("scripts.telegram_bot.send_text", send)

    assert send_telegram_text("Synthetic message") is (send_result is True)
    send.assert_called_once_with("Synthetic message")


def test_delivery_adapter_does_not_log_exception_credentials(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from utils.delivery import send_telegram_text

    # Synthetic marker, not a token: external exceptions may contain private URLs.
    sensitive_marker = "SYNTHETIC_SECRET_DO_NOT_LOG"
    monkeypatch.setattr(
        "scripts.telegram_bot.send_text", MagicMock(side_effect=RuntimeError(sensitive_marker)),
    )

    assert send_telegram_text("Synthetic message") is False
    assert sensitive_marker not in caplog.text


@pytest.mark.parametrize("pipeline", ["training_advisor"], indirect=True)
@pytest.mark.parametrize("generated", ["Synthetic generated plan", None])
def test_training_generation_only_mode_has_no_production_outputs(
    pipeline: SimpleNamespace, generated: Optional[str],
) -> None:
    pipeline.generate.return_value = generated

    result = pipeline.module.run_weekly_advisor(output_telegram=False)

    assert result == (generated or "Synthetic fallback plan")
    pipeline.send.assert_not_called()
    pipeline.alert.assert_not_called()
    pipeline.ledger.assert_not_called()
