"""scripts/garmin_login.py — Garmin Connect 登入 + MFA + 快取 cookie。

一次性執行，成功後 token 快取至 ~/.garminconnect/，
後續所有腳本（health_tracker, test_garmin_coach 等）都能直接用快取 token。

執行方式::

    conda activate life-os
    python scripts/garmin_login.py

流程：
1. 嘗試用快取 token 登入（~/.garminconnect/）
2. 快取失敗 → 用帳密登入
3. 如需 MFA → 提示你輸入 email 收到的驗證碼
4. 登入成功 → token 自動快取
5. 測試幾個 API 端點確認連線正常
"""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def prompt_mfa() -> str:
    """提示使用者輸入 MFA 驗證碼。"""
    print()
    print("=" * 50)
    print("  Garmin 需要 MFA 驗證")
    print("  請查看 email 信箱中的驗證碼")
    print("=" * 50)
    code = input("輸入驗證碼: ").strip()
    return code


def main():
    from garminconnect import Garmin

    email = os.getenv("GARMIN_EMAIL", "")
    password = os.getenv("GARMIN_PASSWORD", "")

    if not email or not password:
        print("❌ GARMIN_EMAIL 或 GARMIN_PASSWORD 未設定，請檢查 .env")
        sys.exit(1)

    print(f"📧 使用帳號: {email}")

    # ── 登入 ──────────────────────────────────────────────────────────
    token_dir = str(Path(__file__).resolve().parent.parent / ".garmin_tokens")
    Path(token_dir).mkdir(exist_ok=True)

    client = Garmin(email, password, prompt_mfa=prompt_mfa)

    try:
        client.login(tokenstore=token_dir)
        print(f"✅ 登入成功！Token 已快取至 {token_dir}")
    except Exception as e:
        print(f"❌ 登入失敗: {e}")
        sys.exit(1)

    # ── 同時存一份 web cookies 格式（給 debug_garmin.py 等用）─────────
    try:
        cookies_dir = Path(__file__).resolve().parent.parent / ".garmin_web_cookies"
        cookies_dir.mkdir(exist_ok=True)

        # 從 session 抓 cookies
        cookie_list = []
        for cookie in client.session.cookies:
            cookie_list.append({
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
            })

        cookies_path = cookies_dir / "cookies.json"
        cookies_path.write_text(
            json.dumps(cookie_list, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"🍪 Web cookies 已更新: {cookies_path}")
    except Exception as e:
        print(f"⚠️ Cookie 匯出失敗（不影響主要功能）: {e}")

    # ── 測試 API 端點 ────────────────────────────────────────────────
    print()
    print("=" * 50)
    print("  API 端點測試")
    print("=" * 50)

    today = date.today()

    # 1. Daily Summary
    try:
        summary = client.get_stats(today.isoformat())
        steps = summary.get("totalSteps", "N/A")
        print(f"✅ 每日摘要: {steps} 步")
    except Exception as e:
        print(f"⚠️ 每日摘要: {e}")

    # 2. Training Plans
    try:
        plans = client.get_training_plans()
        if plans:
            print(f"✅ 訓練計劃: 找到 {len(plans) if isinstance(plans, list) else '?'} 個計劃")
            text = json.dumps(plans, indent=2, ensure_ascii=False, default=str)
            # 印前 1500 字元
            if len(text) > 1500:
                print(text[:1500])
                print(f"... (共 {len(text)} 字元)")
            else:
                print(text)
        else:
            print("⚠️ 訓練計劃: 空回傳（可能沒有啟用 Garmin Coach）")
    except Exception as e:
        print(f"⚠️ 訓練計劃: {e}")

    # 3. Workouts
    try:
        workouts = client.get_workouts()
        if workouts:
            print(f"✅ Workouts: 找到 {len(workouts) if isinstance(workouts, list) else '?'} 個")
        else:
            print("⚠️ Workouts: 空回傳")
    except Exception as e:
        print(f"⚠️ Workouts: {e}")

    # 4. Recent Activities
    try:
        activities = client.get_activities_by_date(
            (today - timedelta(days=7)).isoformat(),
            today.isoformat(),
        )
        if activities:
            print(f"✅ 近 7 天活動: {len(activities)} 個")
            for a in activities[:3]:
                name = a.get("activityName", "?")
                dist = a.get("distance", 0) / 1000
                print(f"   - {name}: {dist:.1f} km")
        else:
            print("⚠️ 近 7 天無活動")
    except Exception as e:
        print(f"⚠️ 活動: {e}")

    # 5. Training Readiness
    try:
        readiness = client.get_training_readiness(today.isoformat())
        if readiness:
            score = readiness.get("score", readiness.get("trainingReadinessScore", "?"))
            print(f"✅ Training Readiness: {score}")
        else:
            print("⚠️ Training Readiness: 空回傳")
    except Exception as e:
        print(f"⚠️ Training Readiness: {e}")

    print()
    print("=" * 50)
    print("  完成！後續腳本可直接使用快取 token")
    print("=" * 50)


if __name__ == "__main__":
    main()
