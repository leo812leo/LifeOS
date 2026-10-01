# Public-source migration scope

Snapshot date: 2026-10-02. Destination: `leo812leo/LifeOS`.

## Copied and sanitized

Core Python application, calculation utilities, test suite, prompt templates and
dashboard source. Personal narrative was removed, profile examples de-identified,
stock holdings emptied, and dotenv lookup restricted to the current checkout.
Tests add an offline guard. The dashboard uses empty hosting bindings and omits
the generated social preview image and original hosting identity.

## Kept in the original local workspace, not uploaded

- Original `.git` history, refs and local agent configuration.
- All credentials, caches, account identifiers, broker certificates and `.env`.
- Health, training, finance, journal and backup exports; logs and pending records.
- Original README/CLAUDE/AGENTS documents, private planning/research/travel notes,
  task briefs, project reviews and presentation deliverables. These were replaced
  with these public documentation files, not silently claimed as migrated.
- Historical one-off Notion setup/migration tools and root Node dependency files.
- Garmin diagnostic/export probes, brokerage setup probes and machine-specific
  Windows launcher/scheduler scripts. They require a separate safety review.
- Generated assets/build output, dependencies, screenshots and `.openai` identity.

No original file was deleted. The original local application and scheduling were
not switched to this public copy. The public copy is a separate source line;
changes need deliberate review and synchronization in either direction.

## Repository versus cloud environment

GitHub stores this reviewed source snapshot. Codex Cloud configuration describes
how development tasks obtain and prepare repositories; it is not a backup of the
local workspace. This publication does not depend on migrating a legacy environment.

## Next migration stage

Before cloud execution, choose hosting and a budget, provision secrets privately,
resolve the listed reliability gaps, verify authorization, dry-run isolation and
backup restoration, then approve an explicit cutover plan. Avoid duplicate jobs
running simultaneously on the PC and in the cloud.
