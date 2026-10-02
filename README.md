# LifeOS

個人生活自動化專案：健康與訓練資料、投資追蹤、日記、定期回顧、Telegram 通知，以及手機友善的監控介面。

這是從本機專案整理的 **公開原始碼快照（2026-10-02）**。不包含任何人的憑證、健康與財務紀錄、原始備份或舊 Git 歷史。公開原始碼不代表公開私人資料，也不代表已完成雲端執行部署。

## 包含什麼

- `config.py`、`models.py`、`notion_client_helper.py`：設定、資料模型、Notion 封裝。
- `scripts/`：Garmin/Notion 同步、投資追蹤、訓練助理、回顧、通知、備份與 watchdog。
- `utils/`、`prompts/`：計算工具及去識別化提示詞。
- `tests/`：使用 mock、禁止真實服務流量的 Python 測試。
- `dashboard/`：React / vinext 手機友善介面；目前未接入即時私人資料。
- `docs/`：公開版架構、限制與後續工作。

## 本機開發

Python 3.12 為本次驗證環境；typing 維持原專案 Python 3.9 寫法。

```sh
python -m venv .venv
# 啟用 .venv 後：
python -m pip install -r requirements-dev.txt
python scripts/test_offline.py
```

Windows 建議設定 `PYTHONUTF8=1`。統一測試入口會在載入 pytest 前隔離 dotenv／服務憑證，阻擋常用網路 transport 及子程序，並將日誌／帳本等 runtime 路徑改用暫存資料夾。Windows asyncio 的標準庫 socketpair 只允許建立自身 loopback 通道。這是防止意外正式服務呼叫的護欄，不是不可信原生程式的安全沙箱；套件安裝仍需網路。

GitHub PR 會執行 Python 離線測試，不會部署或執行正式同步。Runtime 相依套件尚未完整鎖版；不要把測試成功當作升級相容性或正式服務驗證。

## Connector-first 協作

先使用既有 GitHub／Notion connector，不為通用整合再造 API wrapper。
Notion／Heptabase 的客戶端範例與 Claude／Codex 協作流程見
[連接與分工指引](docs/CONNECTORS.md)。範例不含憑證、不會自行啟用；
OAuth、寫入權限與背景排程是三個分開的步驟。

前端需 Node.js >= 22.13.0：

```sh
cd dashboard
npm ci
npm test
npm run dev
```

`.openai/hosting.json` 僅有空資料庫/儲存綁定，不含既有網站識別或部署憑證。匯入此倉庫不會更新既有網站，也不會自動建立雲端資源。

## 連接私人服務前

1. 以 `.env.example` 為模板，在自己的 checkout 建立忽略於 Git 的 `.env`；不要把值貼進 issue、日誌或聊天。
2. 配置私人資料庫、持股與運動員檔案。公開版持股清單是空的，檔案/日期預設值是示範用，不能直接套用到真人。
3. 先閱讀 [安全注意事項](SECURITY.md) 與 [遷移範圍](docs/MIGRATION.md)。後端仍有既存可靠度問題；本批只修正每日／每週建議的 dry-run 正式紀錄與通知副作用。`--dry-run` 仍會讀取服務或呼叫 AI，不能當作離線模式。
4. 未經明確批准，不執行任何真實服務驗證、通知、資料庫遷移或 Windows 排程操作。

此專案不是醫療診斷、用藥處方或投資下單系統。AI 輸出與範例閾值需自行審核。

## 目前不是什麼

- 不是本機資料夾的逐檔完整備份；私人文件、資料與歷史刻意留在原處。
- 不是自動同步：日後本機改動不會自行出現在 GitHub。
- 不是 24 小時雲端自動化：尚未啟用雲端排程或移入正式金鑰。
- 不是已登入的私人網站：前端程式碼本身不會替一般託管平台提供存取控制。

後续進度見 [公開版 Roadmap](docs/ROADMAP.md)。
