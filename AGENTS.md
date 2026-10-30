# Handbook contributions

- Keep English in `editions/YYYY-MM/` and Portuguese in `editions/por/YYYY-MM/`.
  Download chapter text from the official Church source in the requested language;
  translate only project documentation. Do not fabricate historical editions or
  fill Portuguese gaps with English or neighboring editions.
- Commit chapter imports separately for each **edition and language**. Keep code,
  documentation and local automation changes in separate commits from chapter imports.
  Capture metadata updates must also be committed per edition/language; global
  worker bookkeeping belongs in a separate automation-state commit.
- For comparison histories, use `branch.sh <eng|por|all> --repository OWNER/REPO`.
  It creates one chapter snapshot commit per edition in `diff-eng` or `diff-por`
  and annotated tags `eng/YYYY-MM` or `por/YYYY-MM`. Do not mix languages in these
  commits. Incomplete editions must not receive release tags.
- Preserve all existing English tags, including legacy `YYYY-MM` tags and `diff`.
  Never delete, force-update or move a tag, locally or remotely. If tagged content
  differs, stop and explain the mismatch; do not silently retag it.
- Use two-dot comparison links between edition tags. Historical editions may be
  recovered out of order; do not rebuild published history to insert them.
- Save Page Now preserves newly downloaded live pages. Historical recovery only
  queries existing captures and never sends an old edition's URL to Save Page Now.
- Persist queue state before submitting a capture, keep job IDs across retries,
  honor Retry-After and service backoff, and reconcile uncertain POST outcomes
  without blind resubmission. Associate a snapshot with an edition only after
  checking its exact timestamp, source URL, chapter identity, language and content
  fingerprint. Mismatched candidates stay in the queue, not verified source metadata.
- Run all automation locally. Do not add CI/CD workflows or hosted schedulers.
- Credentials belong only in process environment variables or a local secret manager
  (`IA_ACCESS_KEY`, `IA_SECRET_KEY`). Never log or commit their values or auth headers.
- Keep the main README brief, with the language table. Put preservation, history
  generation and credential configuration in `docs/automation.md`.
- Run the language, preservation and history regression tests for related changes.
  Distinguish simulated verification from live evidence; pending captures are not
  successful archives. Local Git tests must use temporary repositories.
- Creating local comparison refs does not authorize publishing them. Push only
  when requested or through an explicitly invoked local command with `--publish`.
