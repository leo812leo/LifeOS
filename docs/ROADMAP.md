# Public roadmap

## Notion write diagnostics — 2026-11-14

- Page creation and updates report the SDK error code when Notion rejects a
  write. This preserves the write failure outcome and avoids logging the
  response body.
- Regression tests exercise both write paths with mocked responses.

## Independent activity sync — 2026-11-09

- After Garmin login, the tracker checks for activities even when Garmin returns
  no steps, sleep or resting heart rate. The health task records its failure
  and exits with a nonzero status after the activity check.
- An offline regression test covers the shared Garmin session, skipped health
  write and separate activity call.

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

## Reject non-numeric historical FX rates — 2026-10-14

- For dated Frankfurter responses, only JSON numeric `rate` values are now
  accepted; booleans (which Python otherwise converts to `1.0`) and numeric
  strings fail closed. Positive finite validation remains in place, and the
  latest-rate route is unchanged.
- Test-first regression: `true` and `"31.5"` both failed before the guard.
  Afterward, 48 investment tests and all 756 isolated public tests passed on
  Windows Python 3.12.13. The original local source suite passed 737 tests.
- Independent code and Python reviewers approved with no findings. Tests use
  mocks; no live service was called. GitHub Linux CI run 38467804723 passed all
  756 tests on head `8945173dab420a38b083ad6200b203bcf4e24dda`. This is a small
  finance-integrity fix during the pre-race freeze,
  not a new integration; no merge or deployment.

## Validate historical FX currency pair — 2026-10-12

- The dated Frankfurter fallback now accepts a rate only when the returned
  date matches the requested snapshot and the response identifies `USD` as
  base and `TWD` as quote. Missing or mismatched fields fail closed.
- Test-first regression coverage: four wrong/missing currency-pair cases failed
  before the fix; 46 investment tests and all 754 isolated public tests passed
  after it on Windows Python 3.12.13. No live services were called.
- Independent code and Python reviews approved; no Critical/High findings.
  The Python 3.9-compatible typing style is retained. Malformed payload and
  invalid numeric cases are optional follow-up coverage.
- GitHub Linux CI run 38175181182 passed all 754 tests on code/documentation
  head `cd6c53a1dab0b1b7bbc9dbcb343182d944f31611`; no production service was
  called. This remains within investment PR #2; not merged or deployed.

## Investment snapshot quote-date alignment — 2026-10-09

- Price and USD/TWD requests now use the intended snapshot date. yfinance
  requests the exact `[date, date + 1 day)` interval and rejects missing or
  mismatched dates; the historical Frankfurter fallback also verifies its
  returned date. Missing quotes fail closed instead of substituting a newer
  market value.
- The daily run continues to target yesterday and keeps its weekend skip. A
  weekday market holiday or missing quote now follows the existing failure
  path; calendar-aware holiday handling and missed-run backfill remain open.
- Targeted investment tests: 42 passed. Full isolated public suite: 750 passed
  on Windows Python 3.12.13. Independent code and Python reviews approved the
  change; no production service was called. GitHub Linux CI run 37991764718
  passed all 750 tests on implementation head
  `ee0b17097e4e46a49d217c6709622b181d739142`; later evidence-only edits do not
  expand that run's test claim.
- Uses the existing yfinance and Frankfurter sources; no dependency or service
  was added. This change belongs to the existing investment PR #2; it is not
  merged or deployed.

## Investment write failure exit status — 2026-10-08

- If the Notion writer reports failure, investment tracking now preserves the
  existing alert and failed-run ledger entry, then exits with status 1. This
  lets schedulers and monitoring distinguish a failed write from success;
  successful writes and the no-database skip path are unchanged.
- The regression test failed before the fix and passes afterward. The isolated
  public snapshot passes all 34 investment tests and all 742 offline tests;
  no production service was called. Independent code and Python reviews passed.
  GitHub Linux CI run 37687140514 passed all 742 tests; remote blobs match the
  staged patch. No production service was called.
- This update is scoped to the existing investment PR #2 and is not merged or
  deployed.

## Investment snapshot completeness — 2026-10-06

- Reject a portfolio snapshot if any configured holding lacks a valid quote;
  stop before Notion access, record a failed run and exit with status 1.
- Treat nonpositive or nonfinite prices/FX as missing data. Invalid primary FX
  uses the existing fallback. Reject invalid aggregate totals instead of writing
  zero in their place, including at the direct Notion writer boundary.
- A failed partial run creates no date record, so a later complete run can write
  the same date. Existing historical partial records are not modified.
- Changes use the connector-first foundation in PR #1; validation and the new PR
  status must be verified separately. No production services are used in tests.
- Local offline validation passed: 737 original-source tests and 742 sanitized
  public-snapshot tests. Independent code and Python reviews approved the patch.
- Historical reconciliation of old partial records and concurrent deduplication
  remain follow-up work.
