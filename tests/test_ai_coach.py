"""tests/test_ai_coach.py — ai_coach 的單元測試（dispatcher + OpenRouter）。"""

from unittest.mock import MagicMock, patch

import pytest

from models import AthleteProfile
from scripts.ai_coach import _build_system_prompt, _call_ai_api, _call_openrouter_api


# ── TestCallAiApiDispatcher ───────────────────────────────────────────────────


class TestCallAiApiDispatcher:
    """_call_ai_api() dispatcher 根據 AI_PROVIDER 路由至正確後端。"""

    def test_defaults_to_anthropic(self) -> None:
        with (
            patch.dict("os.environ", {"AI_PROVIDER": ""}, clear=False),
            patch("scripts.ai_coach._call_claude_api", return_value="claude response") as mock_claude,
            patch("scripts.ai_coach._call_openrouter_api") as mock_or,
        ):
            result = _call_ai_api("sys", "msg")

        mock_claude.assert_called_once()
        mock_or.assert_not_called()
        assert result == "claude response"

    def test_routes_to_anthropic_when_set(self) -> None:
        with (
            patch.dict("os.environ", {"AI_PROVIDER": "anthropic"}),
            patch("scripts.ai_coach._call_claude_api", return_value="claude ok") as mock_claude,
            patch("scripts.ai_coach._call_openrouter_api") as mock_or,
        ):
            result = _call_ai_api("sys", "msg")

        mock_claude.assert_called_once()
        mock_or.assert_not_called()
        assert result == "claude ok"

    def test_routes_to_openrouter_when_set(self) -> None:
        with (
            patch.dict("os.environ", {"AI_PROVIDER": "openrouter"}),
            patch("scripts.ai_coach._call_claude_api") as mock_claude,
            patch("scripts.ai_coach._call_openrouter_api", return_value="gemini ok") as mock_or,
        ):
            result = _call_ai_api("sys", "msg")

        mock_or.assert_called_once()
        mock_claude.assert_not_called()
        assert result == "gemini ok"

    def test_openrouter_uses_env_model(self) -> None:
        with (
            patch.dict("os.environ", {
                "AI_PROVIDER": "openrouter",
                "OPENROUTER_MODEL": "anthropic/claude-3-haiku",
            }),
            patch("scripts.ai_coach._call_openrouter_api", return_value="ok") as mock_or,
        ):
            _call_ai_api("sys", "msg")

        call_kwargs = mock_or.call_args[1]
        assert call_kwargs["model"] == "anthropic/claude-3-haiku"

    def test_anthropic_uses_env_model(self) -> None:
        with (
            patch.dict("os.environ", {
                "AI_PROVIDER": "anthropic",
                "ANTHROPIC_MODEL": "claude-sonnet-4-5",
            }),
            patch("scripts.ai_coach._call_claude_api", return_value="ok") as mock_claude,
        ):
            _call_ai_api("sys", "msg")

        call_kwargs = mock_claude.call_args[1]
        assert call_kwargs["model"] == "claude-sonnet-4-5"

    def test_explicit_api_key_passed_through(self) -> None:
        with (
            patch.dict("os.environ", {"AI_PROVIDER": "anthropic"}),
            patch("scripts.ai_coach._call_claude_api", return_value="ok") as mock_claude,
        ):
            _call_ai_api("sys", "msg", api_key="explicit-key")

        call_kwargs = mock_claude.call_args[1]
        assert call_kwargs["api_key"] == "explicit-key"

    def test_provider_value_is_case_insensitive(self) -> None:
        with (
            patch.dict("os.environ", {"AI_PROVIDER": "OpenRouter"}),
            patch("scripts.ai_coach._call_openrouter_api", return_value="ok") as mock_or,
            patch("scripts.ai_coach._call_claude_api") as mock_claude,
        ):
            _call_ai_api("sys", "msg")

        mock_or.assert_called_once()
        mock_claude.assert_not_called()

    def test_returns_none_when_backend_fails(self) -> None:
        with (
            patch.dict("os.environ", {"AI_PROVIDER": "openrouter"}),
            patch("scripts.ai_coach._call_openrouter_api", return_value=None),
        ):
            result = _call_ai_api("sys", "msg")

        assert result is None


# ── TestCallOpenrouterApi ─────────────────────────────────────────────────────


class TestCallOpenrouterApi:
    """_call_openrouter_api() 的 HTTP 行為測試。"""

    def _mock_success_response(self, content: str = "訓練計劃回應") -> MagicMock:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": content}}]
        }
        return mock_resp

    def test_returns_content_on_success(self) -> None:
        with patch("scripts.ai_coach.requests.post", return_value=self._mock_success_response("計劃A")):
            result = _call_openrouter_api("sys", "msg", api_key="sk-or-test")

        assert result == "計劃A"

    def test_returns_none_when_api_key_missing(self) -> None:
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": ""}):
            result = _call_openrouter_api("sys", "msg", api_key="")

        assert result is None

    def test_returns_none_on_http_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        with patch("scripts.ai_coach.requests.post", return_value=mock_resp):
            result = _call_openrouter_api("sys", "msg", api_key="bad-key")

        assert result is None

    def test_retries_on_rate_limit_then_succeeds(self) -> None:
        rate_limit_resp = MagicMock()
        rate_limit_resp.status_code = 429

        success_resp = self._mock_success_response("重試成功")

        with (
            patch("scripts.ai_coach.requests.post", side_effect=[rate_limit_resp, success_resp]),
            patch("scripts.ai_coach.time.sleep"),  # 不真的等待
        ):
            result = _call_openrouter_api("sys", "msg", api_key="sk-or-test", max_retries=2)

        assert result == "重試成功"

    def test_returns_none_after_max_retries_on_rate_limit(self) -> None:
        rate_limit_resp = MagicMock()
        rate_limit_resp.status_code = 429

        with (
            patch("scripts.ai_coach.requests.post", return_value=rate_limit_resp),
            patch("scripts.ai_coach.time.sleep"),
        ):
            result = _call_openrouter_api("sys", "msg", api_key="sk-or-test", max_retries=2)

        assert result is None

    def test_returns_none_on_timeout(self) -> None:
        import requests as req_lib
        with (
            patch("scripts.ai_coach.requests.post", side_effect=req_lib.exceptions.Timeout),
            patch("scripts.ai_coach.time.sleep"),
        ):
            result = _call_openrouter_api("sys", "msg", api_key="sk-or-test", max_retries=1)

        assert result is None

    def test_sends_correct_model_in_payload(self) -> None:
        with patch("scripts.ai_coach.requests.post", return_value=self._mock_success_response()) as mock_post:
            _call_openrouter_api(
                "sys", "msg",
                api_key="sk-or-test",
                model="google/gemini-2.5-flash",
            )

        payload = mock_post.call_args[1]["json"]
        assert payload["model"] == "google/gemini-2.5-flash"

    def test_sends_system_and_user_messages(self) -> None:
        with patch("scripts.ai_coach.requests.post", return_value=self._mock_success_response()) as mock_post:
            _call_openrouter_api("我是系統提示", "我是用戶訊息", api_key="sk-or-test")

        payload = mock_post.call_args[1]["json"]
        messages = payload["messages"]
        assert messages[0] == {"role": "system", "content": "我是系統提示"}
        assert messages[1] == {"role": "user", "content": "我是用戶訊息"}

    def test_uses_env_api_key_when_not_passed(self) -> None:
        with (
            patch.dict("os.environ", {"OPENROUTER_API_KEY": "env-key"}),
            patch("scripts.ai_coach.requests.post", return_value=self._mock_success_response()) as mock_post,
        ):
            _call_openrouter_api("sys", "msg")  # 不傳 api_key

        headers = mock_post.call_args[1]["headers"]
        assert "env-key" in headers["Authorization"]


# ── TestBuildSystemPrompt ─────────────────────────────────────────────────────


class TestBuildSystemPrompt:
    """_build_system_prompt() — 配速表與心率區間由 VDOT 動態推導（T13）。"""

    def _profile(self, vdot: float = 38.0, max_hr: int = 185) -> AthleteProfile:
        return AthleteProfile(vdot=vdot, max_hr=max_hr)

    def test_no_hardcoded_paces_remain(self) -> None:
        """驗收條件：舊有的寫死配速/心率數字不應再出現在填充後的 prompt。"""
        prompt = _build_system_prompt(self._profile())
        for token in ("5:40", "5:15", "4:50", "4:30"):
            assert token not in prompt

    def test_changing_vdot_changes_full_pace_table(self) -> None:
        """改變 VDOT → 配速全表變動（驗收條件 #2）。"""
        prompt_38 = _build_system_prompt(self._profile(vdot=38.0))
        prompt_42 = _build_system_prompt(self._profile(vdot=42.0))
        assert prompt_38 != prompt_42

        from scripts.ai_coach import _format_pace
        from utils.vdot_paces import vdot_to_paces

        paces_42 = vdot_to_paces(42.0)
        assert _format_pace(paces_42.marathon) in prompt_42
        assert _format_pace(paces_42.marathon) not in prompt_38

    def test_changing_resting_hr_changes_zone_boundaries(self) -> None:
        """mock RHR 變動 → 心率區間邊界變動（驗收條件 #2）。"""
        prompt_low = _build_system_prompt(self._profile(), resting_hr=55.0)
        prompt_high = _build_system_prompt(self._profile(), resting_hr=75.0)
        assert prompt_low != prompt_high

        from utils.vdot_paces import karvonen_zones

        zones_high = karvonen_zones(185, 75.0)
        assert str(zones_high.z1_max) in prompt_high

    def test_missing_resting_hr_falls_back_to_default(self) -> None:
        from utils.vdot_paces import DEFAULT_RESTING_HR

        prompt_none = _build_system_prompt(self._profile(), resting_hr=None)
        prompt_default = _build_system_prompt(self._profile(), resting_hr=DEFAULT_RESTING_HR)
        assert prompt_none == prompt_default
