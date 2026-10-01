# Public roadmap

## Source publication — 2026-10-02

- Sanitized source snapshot prepared from the existing local project.
- Private records, credentials, old history and personal documents excluded.
- Python tests execute offline; no production service checks are implied.
- Dashboard remains unconnected; source publication is not a deployment.

## Before production cloud migration

1. Make dry-run prohibit both notifications and production ledger writes.
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
