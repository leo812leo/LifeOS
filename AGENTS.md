# LifeOS public source

This repository is a sanitized public source snapshot, not a dump of a private
workspace. Read README.md, SECURITY.md and docs/MIGRATION.md first.

- Never print, commit or upload credentials, cookies, private records or backups.
- Never change existing `.env` values without specific permission.
- Tests must use mocks. No real Garmin, Notion, Telegram, OpenRouter or broker traffic.
- Never operate Windows Task Scheduler without explicit approval.
- Preserve Python 3.9-style typing (`Optional`, `List`); use UTF-8 on Windows.
- Keep frozen Notion field names unchanged:
  - Investment: Name, Date, Total TWD, TW Total, US Total, Exchange Rate, Notes.
  - Health: Name, Date, Steps, Resting HR, Sleep Hours, Sleep Score, Stress Avg, Body Battery, Active Calories.
- Do not import the private source repository's old Git history or private documents.
- Test with `python -m pytest tests/ -q`; frontend: `cd dashboard && npm test`.
- Use conventional commits, `codex/` branches when applicable, and no Co-Authored-By trailer.
- Update docs/ROADMAP.md after code changes. Do not mark mock tests as live-service verification.
