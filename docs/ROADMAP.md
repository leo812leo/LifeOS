# Public roadmap

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
- Quote-date alignment, historical reconciliation and concurrent deduplication
  remain follow-up work.

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

