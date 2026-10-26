#!/usr/bin/env python3
"""Generate data/progress.json for one classroom.

Reads (via GitHub Contents API, PROGRESS_PAT):
  - <org>/<config-repo>/<classroom>/scores.json      (collected scores snapshot)
  - <org>/<config-repo>/<classroom>/assignments.json (due dates, names, max)
  - <org>/<config-repo>/<classroom>/roster.csv       (full names)
Fetches live per-student PR review states (Feedback PR #1, last APPROVED /
CHANGES_REQUESTED wins) for every (assignment, student) cell that has a
collected entry. When the last verdict is CHANGES_REQUESTED but the student
re-submitted after that review was posted, the cell gets fix_submitted=true:
such fixes are back "under review" until the teacher re-reviews. Writes
<classroom>/data/progress.json (schema nsu-syspro/progress/v1).

Per-task acceptance (review-body task-list convention): an APPROVED review
accepts every task the autograder scored; a CHANGES_REQUESTED review accepts
exactly the tasks checked in its markdown task list ("- [x] Задание №N");
a CHANGES_REQUESTED review without a task list accepts nothing. Each cell
stores accepted_score (points the teacher has accepted so far) and tasks
(per-task accepted/scored/unscored states for the point-bearing tests), and
each student gets zachet (pass: every past-due assignment has at least
PASS_THRESHOLD accepted points; fail; None = nothing submitted).

Classroom knobs come from <classroom>/publish.json in this repo:
  {
    "enabled": true,               # master switch: publish this classroom
    "mode": "past-due",            # past-due (default) | explicit
    "show_early": false,           # show assignments with submissions
                                   # before their deadline
    "assignments": [],             # explicit mode: only these slugs
    "exclude_students": []         # usernames never shown
  }

The display name comes from the config repo's <classroom>/classroom.json
(field "name"), not from publish.json.

If enabled is false the script exits without writing anything.

Exit codes: 0 ok (or disabled); 1 config/data error; 2 network error.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

ORG = os.environ.get("PROGRESS_ORG", "nsu-syspro")
CLASSROOM = os.environ.get("PROGRESS_CLASSROOM", "mpt")
CONFIG_REPO = os.environ.get("PROGRESS_CONFIG_REPO", "classroom50")
API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = "nsu-syspro/progress/v1"

# Зачет threshold: every assignment needs at least this many ACCEPTED points
# (autograder points that the teacher has actually approved).
PASS_THRESHOLD = 8

# One item of the review-body task list convention for CHANGES_REQUESTED
# verdicts, e.g. "- [x] Задание №3 (3 балла)". The number maps to the test
# named "Task #N" in assignments.json; anything after the number is a comment.
TASK_ITEM_RE = re.compile(
    r"^\s*[-*+]\s+\[([ xX])\]\s*Задание\s*№?\s*(\d+)",
    re.MULTILINE,
)


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def token() -> str:
    tok = os.environ.get("PROGRESS_PAT", "").strip()
    if not tok:
        die("PROGRESS_PAT is not set")
    return tok


_TOK = token()
_HITS = 0


def api(path: str) -> tuple[int, bytes]:
    """GET; returns (http_status, body). Retries transient 5xx a few times."""
    global _HITS
    url = f"{API}/{path}" if not path.startswith("http") else path
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {_TOK}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "classroom50-status",
        },
    )
    for attempt in range(4):
        _HITS += 1
        if _HITS % 20 == 0:
            time.sleep(1.0)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 500, 502, 503, 504) and attempt < 3:
                # 403 may be secondary rate limiting; back off and retry.
                time.sleep(2.0 * (attempt + 1))
                continue
            return exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < 3:
                time.sleep(2.0 * (attempt + 1))
                continue
            die(f"network error fetching {url}: {exc}", 2)
    return 599, b""


def raw_file(repo: str, path: str) -> bytes | None:
    """File content from <org>/<repo> at the default branch; None on 404.

    Uses the media-type raw format so the body is the file itself, and falls
    back to base64-decoding the Contents JSON envelope if a proxy/ghes ignores
    the Accept header.
    """
    enc = urllib.parse.quote(path, safe="/")
    url = f"{API}/repos/{ORG}/{repo}/contents/{enc}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {_TOK}",
            "Accept": "application/vnd.github.raw+json",
            "User-Agent": "classroom50-status",
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
            # Envelope sniff: a real assignments/scores/roster file never starts
            # with '{"name"' (scores/assignments start with '{"schema"').
            if body.startswith(b'{"name":"') or body.startswith(b'{"name": "'):
                envelope = json.loads(body)
                import base64
                return base64.b64decode(envelope.get("content", ""))
            return body
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code in (403, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2.0 * (attempt + 1))
                continue
            die(f"GET {repo}/{path}: HTTP {exc.code}", 1)
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < 2:
                time.sleep(2.0 * (attempt + 1))
                continue
            die(f"network error fetching {url}: {exc}", 2)
    return None


def parse_rfc3339(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None


def load_knobs(classroom: str) -> dict:
    path = os.path.join(ROOT, classroom, "publish.json")
    if not os.path.exists(path):
        # Sensible defaults when a classroom has no knobs file yet.
        return {"enabled": True, "title": classroom, "mode": "past-due",
                "assignments": [], "exclude_students": []}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def select_assignments(entries: list[dict], knobs: dict, now: datetime,
                       scores: dict | None = None) -> list[dict]:
    mode = knobs.get("mode", "past-due")
    show_early = bool(knobs.get("show_early", False))
    out = []
    for entry in entries:
        slug = entry.get("slug")
        if entry.get("locked"):
            continue
        if mode == "explicit":
            if slug in (knobs.get("assignments") or []):
                out.append(entry)
            continue
        due = parse_rfc3339(entry.get("due") or "")
        has_submissions = bool(
            scores and (scores.get(slug) or {}).get("entries")
        )
        if (due is not None and due <= now) or (show_early and has_submissions):
            out.append(entry)
    out.sort(key=lambda e: e.get("due") or "")
    return out


def review_state(slug: str, owner: str, latest_sub: str | None) -> tuple[str, bool, dict | None, set[int] | None]:
    """(review state, fix_submitted, last student comment, accepted tasks).

    fix_submitted is true when the last verdict on the PR is
    CHANGES_REQUESTED and the student's latest submission is newer than that
    review: the student answered the feedback, the cell is awaiting a
    re-review. Compares the submission timestamp (from scores.json, set by
    the grading runner at tag creation) with the review's submitted_at, so a
    teacher-side re-request of changes after a fix correctly clears the
    flag.

    The third value summarizes STUDENT discussion activity on the PR:
    students ask questions / start threads there, and the teacher only sees
    them by opening each PR. Both conversation comments (issues/1/comments)
    and inline review-comment replies (pulls/1/comments) are scanned; the
    latest comment authored by the student is returned as
    {at, body} (body whitespace-collapsed, truncated). None when the student
    never commented.

    The fourth value carries per-task acceptance parsed from the deciding
    review's body (review-body task-list convention):
      - None: no per-task info (APPROVED = everything accepted; no_pr /
        closed / unknown / plain unreviewed = nothing to parse);
      - set of task numbers: CHANGES_REQUESTED; checked items are accepted,
        everything else is not. An empty set means the CR review has no
        task list at all, so no task point is accepted.
    """
    repo = f"{CLASSROOM}-{slug}-{owner}"
    status, body = api(f"repos/{ORG}/{repo}/pulls/1")
    if status == 404:
        return "no_pr", False, None, None
    if status != 200:
        print(f"warning: {repo} PR fetch: HTTP {status}", file=sys.stderr)
        return "unknown", False, None, None
    pr_closed = json.loads(body).get("state") == "closed"
    status, body = api(f"repos/{ORG}/{repo}/pulls/1/reviews?per_page=100")
    if status != 200:
        print(f"warning: {repo} reviews fetch: HTTP {status}", file=sys.stderr)
        return "unknown", False, None, None
    decision = "unreviewed"
    decision_at: str | None = None
    decision_body = ""
    try:
        for review in json.loads(body):
            state = review.get("state")
            if state in ("APPROVED", "CHANGES_REQUESTED"):
                decision = state.lower()
                decision_at = review.get("submitted_at")
                decision_body = review.get("body") or ""
    except json.JSONDecodeError:
        return "unknown", False, None, None
    if decision == "unreviewed" and pr_closed:
        return "closed", False, None, None      # PR closed without a verdict; nothing to review
    accepted_tasks: set[int] | None = None
    if decision == "changes_requested":
        accepted_tasks = {
            int(m.group(2))
            for m in TASK_ITEM_RE.finditer(decision_body)
            if m.group(1).strip()
        }
    fix_submitted = bool(
        decision == "changes_requested"
        and latest_sub
        and decision_at
        and parse_rfc3339(latest_sub) and parse_rfc3339(decision_at)
        and parse_rfc3339(latest_sub) > parse_rfc3339(decision_at)
    )

    last_student_comment: dict | None = None
    for endpoint in (f"repos/{ORG}/{repo}/issues/1/comments",
                     f"repos/{ORG}/{repo}/pulls/1/comments"):
        status, body = api(endpoint)
        if status != 200:
            print(f"warning: {repo} comments fetch: HTTP {status}", file=sys.stderr)
            continue
        try:
            comments = json.loads(body)
        except json.JSONDecodeError:
            continue
        for comment in comments:
            author = ((comment.get("user") or {}).get("login") or "").lower()
            if author != owner.lower():
                continue                      # only the student's own activity
            at = comment.get("created_at") or ""
            text = comment.get("body") or ""
            if last_student_comment is None or at > last_student_comment["at"]:
                last_student_comment = {
                    "at": at,
                    "body": " ".join(text.split())[:120],
                }
    return decision, fix_submitted, last_student_comment, accepted_tasks


def main() -> int:
    now = datetime.now(timezone.utc)
    knobs = load_knobs(CLASSROOM)

    if knobs.get("enabled") is False:
        print(f"{CLASSROOM}: disabled in publish.json, nothing to do")
        return 0

    classroom_raw = raw_file(CONFIG_REPO, f"{CLASSROOM}/classroom.json")
    assignments_raw = raw_file(CONFIG_REPO, f"{CLASSROOM}/assignments.json")
    scores_raw = raw_file(CONFIG_REPO, f"{CLASSROOM}/scores.json")
    roster_raw = raw_file(CONFIG_REPO, f"{CLASSROOM}/roster.csv")
    if assignments_raw is None or scores_raw is None:
        die(f"missing {CLASSROOM}/assignments.json or scores.json in {ORG}/{CONFIG_REPO}")
    classroom_name = CLASSROOM
    if classroom_raw is not None:
        try:
            classroom_name = json.loads(classroom_raw).get("name") or CLASSROOM
        except json.JSONDecodeError:
            print(f"warning: {CLASSROOM}/classroom.json is not valid JSON", file=sys.stderr)
        else:
            print(f"classroom name from classroom50: {classroom_name}")

    all_assignments = json.loads(assignments_raw)["assignments"]
    scores = json.loads(scores_raw)["assignments"]
    published = select_assignments(all_assignments, knobs, now, scores)
    published_slugs = {a["slug"] for a in published}

    names: dict[str, str] = {}
    roster_students: list[str] = []
    staff: set[str] = set()
    if roster_raw:
        for row in csv.DictReader(io.StringIO(roster_raw.decode("utf-8"))):
            username = (row.get("username") or "").strip()
            if not username:
                continue
            first = (row.get("first_name") or "").strip()
            last = (row.get("last_name") or "").strip()
            # Display order: Last name First name (roster stores First Last).
            names[username] = f"{last} {first}".strip()
            if (row.get("role") or "student") == "student":
                roster_students.append(username)
            else:
                staff.add(username)

    excluded = set(knobs.get("exclude_students") or []) | staff

    # Manual verdict overrides: (lowercased username, assignment slug) cells
    # where the teacher accepted the whole assignment without (or contrary
    # to) the PR review state, e.g. a closed PR whose work was accepted.
    overrides = {
        (str(ov.get("student", "")).lower(), ov.get("assignment", ""))
        for ov in (knobs.get("overrides") or [])
        if ov.get("accepted", True)
    }

    # Point-bearing tests per assignment, in order; zero-point "setup" tests
    # are not part of the per-task grid. Task number comes from the test name
    # ("Task #N"), matching the "Задание №N" review-body task list.
    task_meta: dict[str, list[dict]] = {}
    for a in published:
        meta = next((m for m in all_assignments if m["slug"] == a["slug"]), {})
        lst = []
        for t in meta.get("tests", []):
            pts = int(t.get("points", 0) or 0)
            if pts <= 0:
                continue
            name = t.get("name") or ""
            m = re.match(r"Task\s*#?(\d+)$", name)
            n = int(m.group(1)) if m else len(lst) + 1
            lst.append({"task": name or f"Task #{n}", "n": n, "points": pts})
        task_meta[a["slug"]] = lst

    # --- cells ---
    cells: list[dict] = []
    owners_seen: set[str] = set()
    # (slug, owner) -> latest submission datetime; feeds the fix_submitted
    # detection in review_state (student answered a CHANGES_REQUESTED verdict).
    latest_subs: dict[tuple[str, str], str | None] = {}
    jobs: list[tuple[str, str, str | None]] = []
    for slug in sorted(published_slugs):
        bucket = scores.get(slug) or {}
        for entry in bucket.get("entries", []):
            owner = entry.get("owner") or ""
            if owner in excluded:
                continue
            owners_seen.add(owner)
            subs = entry.get("submissions") or []
            latest = max(subs, key=lambda x: x.get("datetime") or "", default={})
            latest_dt = latest.get("datetime")
            latest_subs[(slug, owner)] = latest_dt
            jobs.append((slug, owner, latest_dt))

    with ThreadPoolExecutor(max_workers=4) as pool:
        states = dict(
            zip(
                [(s, o) for s, o, _ in jobs],
                pool.map(lambda job: review_state(*job), jobs),
            )
        )

    for slug in sorted(published_slugs):
        bucket = scores.get(slug) or {}
        tm = task_meta[slug]
        meta = next((a for a in all_assignments if a["slug"] == slug), {})
        assignment_max = sum(t.get("points", 0) for t in meta.get("tests", [])) or 10
        for entry in bucket.get("entries", []):
            owner = entry.get("owner") or ""
            if owner in excluded:
                continue
            # Only the LATEST submission matters for status; submissions are
            # ordered newest-first (subs[0] is the latest), but sort defensively
            # in case that ever changes. NOTE: newest-first means max(), not
            # min(): min() would pin the FIRST attempt, showing a stale
            # low score for anyone who re-submitted.
            subs = entry.get("submissions") or []
            latest = max(subs, key=lambda x: x.get("datetime") or "", default={})
            state, fix_submitted, last_student_comment, accepted_tasks = states.get(
                (slug, owner), ("unknown", False, None, None)
            )
            autograder_score = latest.get("score")
            test_scores = {
                t.get("test-name"): int(t.get("score", 0) or 0)
                for t in (latest.get("tests") or [])
            }
            scored = {t["n"]: test_scores.get(t["task"], 0) > 0 for t in tm}

            # Per-task acceptance + accepted-point total. Task points are
            # always awarded fully or not at all, so an accepted task
            # contributes its full points from assignments.json.
            if (owner.lower(), slug) in overrides:
                # Teacher accepted the whole assignment outside the PR.
                tstates = {t["n"]: "accepted" for t in tm}
                accepted_score = autograder_score
                if accepted_score is None:
                    accepted_score = sum(t["points"] for t in tm)
            elif state == "approved":
                # Whole review accepted: every scored task is accepted.
                tstates = {
                    t["n"]: ("accepted" if scored[t["n"]] else "unscored")
                    for t in tm
                }
                accepted_score = autograder_score or 0
            elif state == "changes_requested" and accepted_tasks:
                # Task-list convention: checked tasks are accepted.
                tstates = {}
                for t in tm:
                    if t["n"] in accepted_tasks:
                        if not scored[t["n"]]:
                            print(
                                f"warning: {CLASSROOM}-{slug}-{owner}: Задание №{t['n']}"
                                " accepted but autograder scored 0",
                                file=sys.stderr,
                            )
                        tstates[t["n"]] = "accepted"
                    else:
                        tstates[t["n"]] = "scored" if scored[t["n"]] else "unscored"
                accepted_score = sum(t["points"] for t in tm if t["n"] in accepted_tasks)
                ghost = accepted_tasks - {t["n"] for t in tm}
                if ghost:
                    print(
                        f"warning: {CLASSROOM}-{slug}-{owner}: review task list"
                        f" references unknown tasks {sorted(ghost)}",
                        file=sys.stderr,
                    )
            else:
                # unreviewed / unknown / closed / no_pr / CR without a task
                # list: nothing accepted yet; squares show what the
                # autograder scored.
                tstates = {
                    t["n"]: ("scored" if scored[t["n"]] else "unscored")
                    for t in tm
                }
                accepted_score = 0

            cell = {
                "student": owner,
                "assignment": slug,
                "score": autograder_score,
                "max_score": latest.get("max-score") or assignment_max,
                "accepted_score": accepted_score,
                "tasks": [tstates[t["n"]] for t in tm],
                "review": state,
                "fix_submitted": fix_submitted,
                "pr_url": f"https://github.com/{ORG}/{CLASSROOM}-{slug}-{owner}/pull/1",
                "release_url": latest.get("release"),
            }
            if last_student_comment is not None:
                # Student discussion activity on the PR (questions, threads).
                # Deliberately NOT rendered on the status page - teacher-side
                # triage signal only, read from progress.json directly.
                cell["last_student_comment"] = last_student_comment
            cells.append(cell)

    students = [u for u in roster_students if u not in excluded]
    students += sorted(owners_seen - set(students) - excluded)

    # Sort by last name; students without a roster name (no last name to sort
    # by) go to the bottom, still sorted among themselves by username.
    def sort_key(username: str) -> tuple[int, str]:
        last = names.get(username, "").split(" ")[0]
        return (0 if last else 1, last or username)

    students.sort(key=sort_key)

    # Зачет: every past-due published assignment needs at least
    # PASS_THRESHOLD accepted points. Students without a single submission
    # get None (the page shows a dash); a missing cell counts as 0.
    acc_by: dict[tuple[str, str], int] = {
        (c["student"], c["assignment"]): c["accepted_score"] for c in cells
    }
    past_due = [
        a["slug"] for a in published
        if (d := parse_rfc3339(a.get("due") or "")) and d <= now
    ]

    def zachet_of(username: str) -> str | None:
        if not any((username, slug) in acc_by for slug in past_due):
            return None
        for slug in past_due:
            if acc_by.get((username, slug), 0) < PASS_THRESHOLD:
                return "fail"
        return "pass"

    doc = {
        "schema": SCHEMA,
        "classroom": CLASSROOM,
        "classroom_title": classroom_name,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "assignments": [
            {
                "slug": a["slug"],
                "name": a.get("name") or a["slug"],
                "due": a.get("due"),
                "max_score": sum(t.get("points", 0) for t in a.get("tests", [])) or 10,
                "collected_at": (scores.get(a["slug"]) or {}).get("collected_at"),
                "submitted": len((scores.get(a["slug"]) or {}).get("entries", [])),
            }
            for a in published
        ],
        "students": [
            {"username": u, "display": names.get(u) or u, "zachet": zachet_of(u)}
            for u in students
        ],
        "cells": cells,
    }

    out_path = os.path.join(ROOT, CLASSROOM, "data", "progress.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print(
        f"{CLASSROOM}/data/progress.json: {len(doc['assignments'])} assignments, "
        f"{len(doc['students'])} students, {len(cells)} cells, {_HITS} API calls"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
