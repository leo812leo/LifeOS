# Connector-first development

這份文件定義程式與工具的分工，不記錄私人資料、憑證或登入狀態。
設定檔存在，不代表已登入、權限足夠、資料同步成功或排程已啟用。

## 先用已有工具，再寫程式

| 資料／責任 | 主要管理位置 | Connector 適合做什麼 | 保留的程式責任 |
| --- | --- | --- | --- |
| 程式、待修問題、變更審查 | GitHub 的清理後原始碼 | 讀檔、分支、PR、查看 CI | 離線測試、安全發布 |
| GTD 任務、專案、生活摘要 | Notion | 查詢、分類草稿、經批准的更新 | 正式匯入、去重、資料完整性 |
| 知識、職涯研究、閱讀 | Heptabase | 搜尋筆記、找關聯、整理研究 | 暫不新增同步程式 |
| 健康、財務數值 | 既有私人資料來源與管線 | 讀取必要摘要 | 計算、日期、缺漏檢查、規則 |
| 時間承諾 | 行事曆 | 查詢、提出時間安排 | 暫不新增排程寫入者 |

一種資料只指定一個主要管理位置、一個正式寫入者。Heptabase 的知識不用
完整複製到 Notion；需要行動的結論才建立任務，保留來源連結。
任務的完整內容、健康數值與帳戶細節不可放進公開 GitHub issue。

## 啟用方式

1. 優先使用客戶端已經連接的官方插件；不要因為 repo 有範例就重複連接。
2. 尚未連接時，先在客戶端選擇官方插件並完成 OAuth。
3. 客戶端需要手動 MCP 設定時，參考 `examples/connectors/` 的兩份範例。
   它們是非活動範例，不會自行載入。只合併需要的 server，勿覆蓋現有設定。
4. OAuth 在各自客戶端完成；不要把權杖、Authorization headers 或密碼填入範例。
5. 在供應商與客戶端支援的範圍限制讀寫權限。Notion MCP 可能繼承使用者的
   Notion 存取權，不要把「請只讀」提示詞當成真正的權限隔離。
6. 真實服務驗證需另獲批准，先指定可讀的頁面與問題；禁止在 pytest 中做。

範例：

- Claude Code：`examples/connectors/claude.mcp.json`，HTTP transport 明確指定。
- Codex：`examples/connectors/codex.config.toml`，只包含公開 server URL。
- GitHub：重用客戶端既有 GitHub connector，不另外建立 PAT 或自製 MCP server。

本階段沒有自動安裝、授權任何服務，也未修改活動中的 MCP 或全域設定。

## 互動 AI 工具不是背景工作

Notion MCP 需互動 OAuth；不能以「已在聊天裡連接」推論 Python 排程已獲授權。
既有 Python 任務目前繼續使用自己的私人 runtime 設定。
若之後改用 Make、n8n 或 Notion Custom Agents，先遷移一個低風險流程，
定義資料日期、去重鍵、重試與通知成功條件，再取得正式切換批准。

Tredict 是 Garmin connector 評估候選，不是本次啟用的依賴。先驗證所需欄位、
歷史資料、成本與使用者授權，才決定是否縮減 Garmin adapter。
Healthchecks 可補外部心跳監控，但本次沒有建立帳號或傳送任何心跳。

## Claude / Codex 共用的開發流程

1. 用公開 issue 描述程式需求：結果、現有工具、檔案 owner、驗收、回復方式。
2. 一個小改動一個 `codex/` 分支；同一檔案指定一個實作者，另一個代理審查。
3. 先新增失敗測試，再修改程式；使用 `python scripts/test_offline.py` 驗證。
4. 在 PR 記錄 RED / GREEN 證據及未驗證事項，不貼私人 log。
5. CI 通過、審查完成後才考慮合併。合併程式不等於批准正式部署或排程切換。

公開 repo 與原私人工作目錄目前是分開的來源線。發布時只使用清理後的
遠端 tree，加上逐檔審查的 patch；不可把私人 config、profile、文件或舊歷史
整份覆蓋到 GitHub。GitHub Actions 只做開發驗證，不放正式服務 secrets，
不執行健康同步、通知或資料庫遷移。

## 每次新增整合前的驗收

- 現有 connector / SDK 是否已能處理？新程式到底補什麼缺口？
- 讀取與寫入分開，是否需要使用者確認？
- 重試會不會重複寫入？有沒有待人工處理的狀態？
- 最後成功時間和資料日期是否分開？缺資料不是零。
- CI 只用假憑證與 mock；本機測試通過不代表正式服務正常。
- 能否停用新流程並回到原寫入者，且不產生雙跑？

## 官方參考（2026-10-03 查核）

- [Notion MCP 與各客戶端設定](https://developers.notion.com/guides/mcp/get-started-with-mcp)
- [Notion MCP 權限](https://www.notion.com/help/notion-mcp)
- [Heptabase MCP 與 OAuth](https://support.heptabase.com/en/articles/12679581-heptabase-mcp)
- [Claude Code MCP](https://code.claude.com/docs/en/mcp)
- [OpenAI 官方 MCP 設定範例](https://developers.openai.com/learn/docs-mcp)
- [Tredict MCP 工具範圍](https://www.tredict.com/blog/mcp_server_docs/)

這些文件描述供應商能力，不證明任何特定帳號已授權或背景執行已就緒。
