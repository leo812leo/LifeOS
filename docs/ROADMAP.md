# Public roadmap

## Connector-first foundation — 2026-10-03

- Add an offline test launcher, temporary runtime paths and guards against
  accidental credential loading, live service calls and production ledger writes.
- Share confirmed Telegram delivery handling across daily, weekly and training
  advice. Failed or unconfirmed delivery is not a successful notification run.
- Keep dry-run out of production ledgers/advice and suppress fallback alerts.
  Dry-run may still read services or invoke AI; only the test launcher is offline.
- Add inactive official Notion/Heptabase MCP examples and connector ownership
  guidance. No client authorization or production integration is implied.
- Add GitHub PR/issue templates and a Python-only CI workflow with read-only
  repository permissions, no service secrets and no deployment steps.
- Offline verification: 710 public-snapshot tests passed on Windows Python 3.12;
  the original local source passed 705 tests. The new delivery helper has 100%
  coverage and the new offline launcher has 82% coverage. The existing large
  advice modules do not yet meet 80% full coverage.
- Independent review fixes include generation-only ledger isolation and avoiding
  credential-bearing Telegram exception/response bodies in logs. Cloud CI results
  must be reported separately; no live-service verification is implied.

- The first clean Linux CI run exposed an unbounded Notion SDK major upgrade.
  Direct runtime dependencies now use the tested versions; transitive locking
  remains follow-up work. This does not upgrade the existing local environment.
- Offline temporary-directory cleanup now validates the registered root and uses
  absolute paths, avoiding Linux fd-relative audit ambiguity without disabling
  IO guards or following symlink/junction targets.

Transitive dependency locking, finance completeness/date corrections, activity
reconciliation, external monitoring and live dashboard data remain separate work.

## Source publication — 2026-10-02

- Sanitized source snapshot prepared from the existing local project.
- Private records, credentials, old history and personal documents excluded.
- Python tests execute offline; no production service checks are implied.
- Dashboard remains unconnected; source publication is not a deployment.

## Before production cloud migration

1. Extend dry-run write isolation to any remaining entry points; never treat it as offline.
2. Separate compute, storage, notification delivery and freshness status.
3. Make market snapshots date-aware, complete and idempotently correctable.
4. Fail closed on Notion deduplication failures; reconcile pending activity reports.
5. Validate training intensity limits structurally; remove implicit personal defaults.
6. Verify per-pipeline monitoring, backup exit codes and restoration.
7. Add authenticated read-only data access and freshness metadata to the dashboard.
8. Select hosting, private secret management and deployment/rollback procedures.

## Longer-term architecture

Health, finance and time are separate domains sharing task review and system
observability. Keep human GTD tasks separate from machine jobs. Notion can hold
structured records and actions; Heptabase can hold knowledge and relationships.
Calendar records actual time commitments. Define one owner per data type before
adding bidirectional synchronization. These are design directions, not implemented integrations.

## Activity lookup and run-ledger safeguards — 2026-10-18

- Activity deduplication now requests the existing Notion helper's explicit
  error mode. A failed lookup returns failure before page creation instead of
  treating an unavailable query as evidence that the activity does not exist.
  Existing/new/failed result semantics and health/activity isolation remain.
- Run-ledger reads stream the physical tail with bounded retained memory, skip
  non-object JSON, return no records for non-positive limits, and accept only
  a boolean success flag with a valid string timestamp as a last success.
  Existing timestamp timezone conventions are unchanged.
- Test-first evidence: ledger RED 4 failed / 14 passed, then GREEN 18 passed;
  expanded ledger coverage passes 43 tests with 100% module line coverage.
  Activity RED 7 failed / 43 passed, then GREEN 50 passed. Both independent
  code and Python reviews approved. The complete public snapshot passes
  749 isolated tests; the original local source passes 776.
- This five-file patch is based on PR #1's tested public source, independent
  of the finance changes in PR #2. It contains no runtime records, private
  configuration or history. Tests use mocks; no live services were called.
  Source validation does not establish a live-service repair or replay any
  historical activity. No merge, migration or scheduler change is included.

## Confirmed feedback writes and reply failures — 2026-10-18

- A confirmed Notion update followed by a Telegram reply error no longer adds
  false pending feedback or attempts a second reply. Metric and fuel handlers
  retain their existing behavior for errors before a confirmed write. The
  post-write error log contains only the exception class.
- Test-first regression: 6 failed / 74 passed, then all 80 Telegram tests passed.
  Independent code and Python reviews approved. The complete local source
  passes 791 isolated tests; the public snapshot passes 764. No live service
  was called. The earlier 749-test run above covers the preceding source head.
- Existing pending records and ambiguous write outcomes remain separate work.
  This adds two module/test files to the same review scope (seven files total
  relative to PR #1), with no replay, storage migration or scheduler changes.
