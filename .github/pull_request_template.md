## Outcome

What user-visible problem does this solve? Link the issue and identify the owner.
Do not paste private records, credentials, account IDs or production logs.

## Reuse first

- Existing connector, SDK or helper considered:
- Why this change still needs code:
- Single writer / source of truth:

## Verification

- [ ] Added a regression test and observed its intended failure before the fix.
- [ ] Ran `python scripts/test_offline.py` with no production service traffic.
- [ ] Listed exact passing checks and anything not verified.
- [ ] Updated the public roadmap without copying private planning documents.
- [ ] Reviewed the diff for secrets and personal data.

## Execution boundaries

- [ ] No existing `.env` values changed.
- [ ] No scheduler, production connector, database schema or deployment changed.
- [ ] No private Git history, records, backups or generated outputs included.

If a boundary must change, describe the separate user approval and cutover plan.

## Rollback / remaining work

Explain how to revert the code and whether any external action actually occurred.
Tests passing is not proof of a working live connection or production deployment.
