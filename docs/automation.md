# Edition history and Wayback preservation

This project has two independent archive operations:

- Historical recovery reads **existing** captures of official Church pages.
- Preservation submits **new live downloads** to Save Page Now and verifies the
  independently collected result. It cannot recreate an old edition from today's URL.

English editions stay in `editions/YYYY-MM/`; Portuguese editions stay in
`editions/por/YYYY-MM/`. There is no permanent `current` directory.

## Credentials

Save Page Now's authenticated API uses Internet Archive S3 access keys, available
from the [account's S3 key page](https://archive.org/account/s3.php).
Provide `IA_ACCESS_KEY` and `IA_SECRET_KEY` through the process environment or a
secret manager. The application never reads credentials from a project file and
never writes their values or authorization headers to logs, queues or source records.

Configure these variables in the local process environment. Do not put credentials
in project files, command arguments or commit messages.

Without credentials, validated downloads remain on disk and captures remain
pending with `credentials_missing`. Reading existing archived pages does not
require these Save Page Now credentials.

## Download and preserve a new edition

Install with `uv sync` or `python3 -m venv .venv` followed by
`.venv/bin/python -m pip install -e .`.

Before adding a new date to `src/handbook/const.py`, check the official update
summary in the relevant language. The most recent configured edition is fetched
live; older dates must be recovered through archived captures.

For example, while October 2026 is the configured latest edition:

```sh
.venv/bin/python execute.py --lang eng download --date 2026-10 --refresh
.venv/bin/python execute.py --lang eng sanitize --date 2026-10
.venv/bin/python execute.py --lang por download --date 2026-10 --refresh
.venv/bin/python execute.py --lang por sanitize --date 2026-10
```

Every new download validates the source URL, chapter identity and chapter language
before saving or queuing it. Both languages now receive `sources.json` and, for
live downloads, `capture-queue.json` in their own dated directory. Legacy English
files without source records are not assumed to be newly validated downloads;
refresh the **latest** edition to preserve it. Never refresh an old edition from
the live site.

With credentials configured, each downloaded chapter advances the queue for up to
ten seconds, so capture requests can start while other chapters are downloading.
After the download pass, the CLI follows queued jobs for up to 300 seconds by default.
`download --archive-budget SECONDS` changes that budget. It may expire before all
captures finish; this does not discard downloads or queued jobs. The download
command's exit status describes the downloads; the separate `archive` command
returns nonzero while captures remain unresolved.

To resume preservation without downloading again:

```sh
.venv/bin/python execute.py archive --all-languages --max-seconds 900
# Or one language/edition:
.venv/bin/python execute.py --lang por archive --date 2026-10 --max-seconds 300
```

## What the capture queue guarantees

Each durable queue key identifies the language, edition, chapter and normalized
content fingerprint. Repeated downloads of the same content reuse the existing
job or verified capture. A worker lock serializes processing. Submissions are at
least ten seconds apart; HTTP requests are spaced, polling is bounded, and server
limits use persistent backoff and `Retry-After`. A worker time budget limits each
invocation without deleting pending work.

The worker persists its submission intent **before** sending the POST, then saves
the returned job ID. It polls that job after restarts instead of submitting it
again. A confirmed failed job may be retried after backoff. Authentication errors,
rate limits and service outages leave explicit pending state.

A lost response, process crash during POST, or ambiguous server failure may mean
the service accepted the capture. Such submissions become `uncertain`. The worker
queries availability and validates a candidate before considering them resolved.
It never blindly repeats an uncertain POST. If no matching capture becomes
available, the uncertainty remains for operator review. Save Page Now does not
provide a client idempotency key, so exactly-once submission cannot be promised.

A successful job response is only a **candidate**, not proof of preservation.
The worker downloads the exact raw replay (`timestamp` plus `id_`) and rejects
redirected replays, a different chapter, a different `lang`, or different content.
The same HTML validation and conversion are used for live and archived pages.
Content comparison normalizes Unicode and whitespace and ignores Markdown link
destinations, which the local sanitizer rewrites; chapter wording, link labels,
headings and table content must match. The original HTML SHA-256 is retained
separately and is not expected to match a separately collected HTML document.

Only after verification does `sources.json` receive `wayback.url`,
`wayback.timestamp`, `wayback.content_sha256` and `wayback.verified_at`, with
`status: verified`. Mismatched candidates remain in the queue for inspection and
are never recorded as verified captures of the current text. If the text changes
while an accepted job is running, its verified result is retained under
`previous_captures`, without replacing the current text's capture record.

Queue states include `pending`, `submitted`, `uncertain`, `verifying`, `verified`,
`mismatch`, `superseded` and `rejected`. Preserve the queue and source records in
Git. Review `mismatch`/`rejected` rather than deleting them to trigger blind retries.

## Separate commits, tags and comparison links

`AGENTS.md` requires chapter and capture-metadata commits to be separated by
language **and** edition. Code and documentation changes belong in separate
commits. The comparison generator creates isolated chapter histories without
switching the checkout, using the main index, moving files, or running `git stash`:

```sh
./branch.sh all --repository OWNER/REPO --dry-run
./branch.sh all --repository OWNER/REPO
# One language only:
./branch.sh por --repository OWNER/REPO
```

The generator creates:

| Language | Comparison branch | New annotated tags | Comparison index |
| --- | --- | --- | --- |
| English | `diff-eng` | `eng/YYYY-MM` | [English comparisons](comparisons-eng.md) |
| Portuguese | `diff-por` | `por/YYYY-MM` | [Portuguese comparisons](comparisons-por.md) |

Each comparison commit contains the chapter Markdown for exactly one language and
edition under the same `handbook/` path. As in the original comparison script,
link destinations are omitted in the generated snapshot to reduce URL noise;
the dated source files keep their working links. Capture status and provenance stay in the
original dated directory, so bookkeeping does not dominate the text comparison.
A tag denotes a complete local chapter snapshot; it does **not** claim that every
Wayback capture has finished. Check `sources.json` for that independent status.

Incomplete editions are skipped. Portuguese source fingerprints are checked
before tagging; existing legacy English snapshots remain supported. Once a tag
exists, different chapter content is a conflict: the generator stops instead of
moving the tag. Existing English `YYYY-MM` tags and the original `diff` branch
are never deleted or rewritten. New namespaced tags coexist with them.

Older editions can be recovered after newer ones have been tagged. They are
appended as new import commits, preserving published history. Generated links
use [GitHub's two-dot comparisons](https://docs.github.com/en/pull-requests/reference/branches#three-dot-and-two-dot-git-diff-comparisons)
to compare the two snapshots directly, independent of import order.

Tags and branches are local until published. Fetch existing comparison branches
and tags before adding to an existing history. A pull request does not transfer
local Git tags: a maintainer must run the generator and publish its refs in the
repository where the comparison links should resolve. Use that repository's
`OWNER/REPO` when generating links.

## Integrated local automation

All automation runs locally through the commands below. There are no CI/CD
workflows or hosted scheduled jobs. Run `sync` explicitly for a newly confirmed
edition and `resume` whenever pending captures need another processing pass.

```sh
# Resume captures, create/update local comparison refs, leave main-tree edits uncommitted:
.venv/bin/python scripts/preserve.py --mode resume --repository OWNER/REPO

# Refresh the latest edition in both languages, follow captures, and commit each edition separately:
.venv/bin/python scripts/preserve.py --mode sync --language all --repository OWNER/REPO --commit

# Explicit publication, with no force updates:
.venv/bin/python scripts/preserve.py --mode resume --repository OWNER/REPO --commit --publish
```

`--commit` refuses an already staged index. Each dated directory gets its own
commit; comparison indexes and global worker scheduling have separate bookkeeping
commits. Unrelated code and documentation changes are left unstaged. `--publish`
uses an atomic push of the active branch, comparison branches and namespaced tags.
It does not push or rewrite legacy tags, bypass protected branches, or force a
non-fast-forward update. A failed push leaves local work available for review.

## Historical recovery remains separate

```sh
./run.sh por                 # try all configured Portuguese editions
./run.sh por 2026-03          # one historical interval
```

Historical downloads validate the archived source, retain its exact timestamp
and never submit its present-day URL to Save Page Now. Portuguese gaps are not
filled with English or neighboring editions. `download-status.json` distinguishes
incomplete searches from an unavailable archive and dates still pending.
`missing.txt` lists chapters absent locally, including dates not yet searched.

## Verification and API evidence

```sh
.venv/bin/python -m unittest discover -s tests
postman simulation run postman/simulations/handbook-preservation.sim.yaml --port 4018 --no-history
# In a second terminal:
.venv/bin/python scripts/test_capture_simulation.py
```

The local Postman simulation models accepted jobs, pending status, matching and
mismatched captures, language mismatches, rate limits and uncertain submissions.
It is based on the [official SPN2 documentation](https://docs.google.com/document/d/1Nsv52MvSjbLb2PCpHlat0gkzw0EvtSgpKHu4mk0MnrA/)
linked from [Save Page Now](https://web.archive.org/save), and the Internet Archive's
[Open Library implementation](https://github.com/internetarchive/openlibrary/blob/master/openlibrary/core/ia.py).
The contract subset and simulation evidence are in `postman/`.

These tests verify this client's behavior against a controlled local service.
They do not prove that a live capture was accepted or completed; real confirmation
requires configured credentials, a successful job and a verified replay.
