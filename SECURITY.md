# Security and operating boundaries

This public repository contains source code and synthetic examples only.

## Never publish

`.env`, service/API credentials, OAuth token caches, cookies, broker certificates,
personal database IDs, exported health/financial records, logs, backups or private
planning documents. `.gitignore` is a convenience, not a security guarantee.

Review every staged diff before publishing. A suspected exposed credential must
be revoked or rotated at its provider; deleting a Git file is not sufficient.
Do not post secrets in public vulnerability reports.

## Do not run as a production deployment yet

- Some legacy `--dry-run` error paths can still send notifications or write ledgers.
- Success receipts do not consistently distinguish computation, storage and delivery.
- Missing/stale market data and deduplication errors need explicit fail-closed handling.
- Training logic includes example thresholds and medication-related rules; it must
  be reviewed and privately configured before applying to any person.
- The dashboard is an unconnected interface. Header-based ChatGPT helpers are only
  suitable behind their trusted hosting gateway, never as independent authentication.
- Before connecting real data, add and verify server-side authorization, access
  restrictions, secret storage, independent monitoring and backup restoration.

The repository upload does not enable production jobs, send messages, migrate
Notion schemas, or change the permissions of any existing service.
