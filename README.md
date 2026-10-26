# classroom50-status

Student-facing progress pages for classroom50 classrooms:

- root: https://nsu-syspro.github.io/classroom50-status/ (classroom list)
- mpt:  https://nsu-syspro.github.io/classroom50-status/?classroom=mpt
  (also `/mpt/`, via a redirect stub)

One common page, one directory per classroom. The page's design and layout
are shared; classroom directories hold only JSON.

## Layout

```
index.html                       common page (list + per-classroom view)
classrooms.json                  index data (generated)
mpt/
  publish.json                   per-classroom knobs (edited by humans)
  data/progress.json             collection data (generated)
  index.html                     optional redirect stub
scripts/
  generate_progress.py           scores + roster + PR states -> progress.json
  build_index.py                 classroom dirs -> classrooms.json
  list_classrooms.py             enabled classrooms -> workflow matrix
  compare_progress.py            content comparison (timestamps ignored)
  validate_json.py               schema validation for all JSON files
schemas/
  publish.schema.json            format of <classroom>/publish.json
  progress.schema.json           format of <classroom>/data/progress.json
  classrooms.schema.json         format of classrooms.json
.github/workflows/
  publish-progress.yaml          nightly 03:00 NSK + dispatch: collect ->
                                 generate -> commit if content changed -> index
  validate.yaml                  every push: JSON schemas + actionlint +
                                 jsdom render tests (tests/)
```

## publish.json format

Edited by humans. Full reference with per-field docs lives in
[`schemas/publish.schema.json`](schemas/publish.schema.json); keep the
`$schema` line at the top so your editor validates and autocompletes it
(VS Code and most editors do this out of the box).

```json
{
  "$schema": "../schemas/publish.schema.json",
  "enabled": true,
  "mode": "past-due",
  "show_early": true,
  "assignments": [],
  "exclude_students": ["aleksiwithlove", "liontiger23"]
}
```

| Field             | Type           | Default     | Meaning |
|-------------------|----------------|-------------|---------|
| `enabled`         | bool           | (required)  | master switch: `false` = no collection, no page data refresh, hidden from the classroom list; data files stay in place |
| `mode`            | string         | `past-due`  | `past-due`: assignments whose deadline has passed (locked ones always skipped); `explicit`: only slugs in `assignments` |
| `show_early`      | bool           | `false`     | `past-due` mode: also show assignments that already have submissions, before their deadline |
| `assignments`     | array of slugs | `[]`        | used in `explicit` mode only |
| `exclude_students`| array of names | `[]`        | usernames never shown; roster staff is excluded automatically |

The display name (page header, classroom list) comes from the config repo's
`<classroom>/classroom.json` field `name`; it is not configured here.

## Adding a classroom

1. Create the directory with two files:

   `bash/publish.json`:
   ```json
   {
     "$schema": "../schemas/publish.schema.json",
     "enabled": true
   }
   ```

   `bash/index.html` (optional redirect stub so `/bash/` works; copy as-is
   and replace the slug):
   ```html
   <!DOCTYPE html>
   <meta charset="utf-8">
   <title>bash</title>
   <script>location.replace("../?classroom=bash");</script>
   ```

2. That's it. The publish workflow's matrix is built from the directories
   (`scripts/list_classrooms.py`), so the next run collects scores, generates
   `bash/data/progress.json` and adds the classroom to the list. The
   validate workflow checks your JSON before that.

## Pausing a classroom

Set `"enabled": false` in its `publish.json`. It drops out of the matrix
(no collection, no refresh) and disappears from the classroom list; existing
`data/progress.json` stays in the repo and is simply not shown.

## Data freshness and commits

- The workflow runs nightly at 03:00 Novosibirsk (20:00 UTC) and on demand
  (Actions tab -> Publish progress -> Run workflow).
- Each run dispatches the classroom50 score collection first, so published
  scores are fresh; then regenerates progress.json.
- A commit happens **only when content changes** (scores, review states,
  students); `generated_at`/`collected_at` timestamps are ignored in the
  comparison. Whether a refresh attempt succeeded is visible in the Actions
  runs, not in the commit log.
- Review states (проверено / есть замечания / на проверке) are fetched live
  from the Feedback PRs at generation time.
- Per-task acceptance follows the review-body task-list convention: an
  APPROVED review accepts every task the autograder scored; a
  CHANGES_REQUESTED review accepts exactly the tasks checked in its markdown
  task list (`- [x] Задание №N (3 балла)`); a CHANGES_REQUESTED review
  without a task list rejects everything. Each cell stores `accepted_score`
  (teacher-accepted points) and `tasks` (per-task states for the
  point-bearing tests). The page shows `accepted_score` / autograder score
  and a colored square per task: green = accepted, red = the review
  rejected the task (на доработку), yellow = submitted and awaiting review
  (first submission or a resubmitted fix), gray = auto-tests do not pass.
- Each student gets `zachet`: pass when every past-due published assignment
  has at least 8 accepted points, fail otherwise, null (dash) when the
  student has no submissions at all.
- Manual verdict overrides live in `publish.json` (`overrides`, see
  `schemas/publish.schema.json`): (student, assignment) cells where the
  teacher accepted the whole assignment outside the PR flow (e.g. a closed
  PR whose work was accepted) are treated as fully accepted.

## Validation and tests

- JSON formats: all three JSON kinds are checked against JSON Schema
  (draft 2020-12) on every push and PR. `publish.json` files carry a
  `$schema` pointer, so editors validate them as you type. Locally:
  `python3 scripts/validate_json.py` (pip install jsonschema).
- Render tests: `tests/` renders index.html with jsdom against the real
  JSON data (both views: classroom list and classroom table) and asserts
  headers, badges, links, footer and absence of rendering artifacts.
  Locally: `npm ci && npm test` in `tests/` (needs Node 18+).
- Workflows: actionlint on every push.
- `tests/fetch_live.mjs` additionally checks the deployed site; run it
  manually after publishing (`node tests/fetch_live.mjs`).

## Secrets

`PROGRESS_PAT` - fine-grained PAT with Contents RW, Actions RW,
Pull requests R, Members R on the org. Rotated by the teacher; when it
expires the nightly run fails at the collect step (visible in Actions).

## Maintainer workflow (avoiding push conflicts)

The publish workflow commits generated files to `main` on its own (nightly
schedule + manual dispatches). Local edits therefore follow this order:

1. `git pull --rebase` before starting work.
2. Commit source changes (page, scripts, schemas, publish.json) first.
3. Only then dispatch publish runs to verify; after a run finishes,
   `git pull --rebase` again before pushing anything.
4. Never commit locally regenerated `data/progress.json` / `classrooms.json`
   - the bot owns them; local copies are test fixtures only. If a rebase
   conflicts in a generated file, take the remote side and regenerate
   locally if the code change affects output.
