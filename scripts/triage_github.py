#!/usr/bin/env python3
"""Look over open PRs and issues once a night, and say what each one needs.

An unanswered PR is how a repo teaches people not to bother. This runs on a timer, reads
every open PR and issue through `gh`, checks the things a maintainer checks first, asks
Jev how urgent each one is, and writes a report with a draft reply for anything that needs
a person.

**It does not post anything.** Not a comment, not a label, not a close. It has a
`--post-acks` flag that will acknowledge brand-new items, and that flag is off, because a
bot that writes in your name to a stranger who has just given you their work is a bad
first impression of a project that wants collaborators. The value here is the report: it
turns "seven open things, I don't know which matter" into "this one is a security report,
this one is stale, these three are fine to merge", in about a minute of reading.

    python3 scripts/triage_github.py --repo owner/name
    python3 scripts/triage_github.py --repo owner/name --json > report.json

Needs `gh` authenticated. Jev is optional: with no key the urgency column is absent and
everything else works.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GH = os.environ.get("GH_BINARY") or "gh"
TIMEOUT = 60
BODY_CHARS = 2_000
STALE_DAYS = 7

# Words that mean "a person has to look at this tonight", checked before Jev so the answer
# does not depend on a network call.
URGENT_WORDS = ("vulnerability", "security", "exploit", "injection", "leak", "leaked",
                "credential", "api key", "password", "private data", "data loss", "corrupt")


def gh_json(args: List[str]) -> Any:
    """`gh` with JSON out. Any failure is an empty answer, never an exception."""
    try:
        done = subprocess.run([GH] + args, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"[triage-github] gh failed: {type(error).__name__}", file=sys.stderr)
        return None
    if done.returncode != 0:
        print(f"[triage-github] gh exited {done.returncode}: {(done.stderr or '')[:200]}", file=sys.stderr)
        return None
    try:
        return json.loads(done.stdout or "null")
    except ValueError:
        return None


def age_days(stamp: str, now: Optional[float] = None) -> float:
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0.0
    return ((now or time.time()) - when.timestamp()) / 86400.0


def _text(item: Dict[str, Any]) -> str:
    return f"{item.get('title') or ''}\n{(item.get('body') or '')[:BODY_CHARS]}"


def looks_urgent(item: Dict[str, Any]) -> Optional[str]:
    lowered = _text(item).lower()
    for word in URGENT_WORDS:
        if word in lowered:
            return word
    return None


def checks_of(pr: Dict[str, Any]) -> Dict[str, Any]:
    """CI state, reduced to what a maintainer wants at a glance."""
    rollup = pr.get("statusCheckRollup") or []
    states = [str(c.get("conclusion") or c.get("state") or "").upper() for c in rollup]
    failing = [s for s in states if s in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED")]
    running = [s for s in states if s in ("PENDING", "IN_PROGRESS", "QUEUED", "")]
    return {"total": len(states), "failing": len(failing), "running": len(running),
            "state": "failing" if failing else ("running" if running else ("passing" if states else "none"))}


def read_pr(pr: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    checks = checks_of(pr)
    comments = pr.get("comments") or []
    reviews = pr.get("reviews") or []
    author = ((pr.get("author") or {}).get("login")) or ""
    answered = any(((c.get("author") or {}).get("login") or "") != author for c in comments) or bool(reviews)
    row = {
        "kind": "pr", "number": pr.get("number"), "title": pr.get("title") or "",
        "author": author, "url": pr.get("url"), "draft": bool(pr.get("isDraft")),
        "age_days": round(age_days(pr.get("createdAt") or "", now), 1),
        "quiet_days": round(age_days(pr.get("updatedAt") or "", now), 1),
        "additions": pr.get("additions"), "deletions": pr.get("deletions"),
        "files": len(pr.get("files") or []), "checks": checks,
        "mergeable": pr.get("mergeable"), "answered": answered,
        "urgent_word": looks_urgent(pr),
    }
    row["needs"] = _needs_pr(row)
    return row


def _needs_pr(row: Dict[str, Any]) -> List[str]:
    needs = []
    if row["urgent_word"]:
        needs.append(f"mentions {row['urgent_word']}: read it first, and move it private if it is a real report")
    if not row["answered"] and not row["draft"]:
        needs.append(f"nobody has replied in {row['quiet_days']:.0f} day(s)")
    if row["checks"]["state"] == "failing":
        needs.append("CI is failing: say which test, so the author does not have to guess")
    if row["mergeable"] == "CONFLICTING":
        needs.append("conflicts with main: it may need a rebase, and if main was rewritten that is our doing")
    if row["checks"]["state"] == "none" and not row["draft"]:
        needs.append("no checks ran at all: a first-time contributor's workflow may need approving")
    if row["quiet_days"] >= STALE_DAYS and row["answered"]:
        needs.append(f"waiting {row['quiet_days']:.0f} days since anyone touched it")
    return needs


def read_issue(issue: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    author = ((issue.get("author") or {}).get("login")) or ""
    comments = issue.get("comments") or []
    answered = any(((c.get("author") or {}).get("login") or "") != author for c in comments)
    row = {
        "kind": "issue", "number": issue.get("number"), "title": issue.get("title") or "",
        "author": author, "url": issue.get("url"),
        "age_days": round(age_days(issue.get("createdAt") or "", now), 1),
        "quiet_days": round(age_days(issue.get("updatedAt") or "", now), 1),
        "labels": [l.get("name") for l in (issue.get("labels") or [])],
        "answered": answered, "urgent_word": looks_urgent(issue),
    }
    needs = []
    if row["urgent_word"]:
        needs.append(f"mentions {row['urgent_word']}: read it first, and move it private if it is a real report")
    if not answered:
        needs.append(f"nobody has replied in {row['quiet_days']:.0f} day(s)")
    elif row["quiet_days"] >= STALE_DAYS:
        needs.append(f"waiting {row['quiet_days']:.0f} days since anyone touched it")
    row["needs"] = needs
    return row


# ── how urgent, per item ─────────────────────────────────────────────────────

LEVELS = {
    "now": "someone is blocked, exposed, or has been waiting long enough to give up on this project",
    "today": "a contributor is waiting on an answer only the maintainer can give",
    "this_week": "real work, nobody is blocked",
    "whenever": "fine as it is; no reply would cost anything",
}


def rank(rows: List[Dict[str, Any]], timeout: float = 8.0, transport: Any = None) -> Dict[str, Any]:
    """Ask Jev how urgent each item is. No key, no network, no problem: the report is the same minus this column."""
    if not rows:
        return {"status": "empty", "ranked": 0}
    try:
        from jevkit import client, privacy
    except ImportError:
        return {"status": "no_jevkit", "ranked": 0}
    sendable, state = [], {}
    for row in rows:
        if privacy.is_sensitive(row["title"]):
            continue
        sendable.append(row)
        state[f"i{row['number']}"] = {
            "kind": row["kind"], "title": privacy.redact(row["title"], 200),
            "days_since_anyone_replied": row["quiet_days"], "answered": row["answered"],
            "needs": row["needs"],
            **({"checks": row["checks"]["state"], "size_lines": (row.get("additions") or 0) + (row.get("deletions") or 0)}
               if row["kind"] == "pr" else {}),
        }
    if not sendable:
        return {"status": "nothing_sendable", "ranked": 0}
    questions = {f"i{row['number']}": client.choice(
        f"How soon does item i{row['number']} need a person?", LEVELS) for row in sendable}
    try:
        reply = client.ask(state, questions, timeout=timeout, transport=transport)
    except Exception as error:  # noqa: BLE001 - a nightly report must not die on a model call
        return {"status": "fail_open", "reason": type(error).__name__, "ranked": 0}
    for row in sendable:
        answer = (reply.get("answers") or {}).get(f"i{row['number']}") or {}
        row["urgency"] = answer.get("choice")
        row["confidence"] = answer.get("confidence")
    return {"status": "ok", "ranked": len(sendable), "latency_ms": reply.get("latency_ms")}


# ── the report ───────────────────────────────────────────────────────────────

ORDER = {"now": 0, "today": 1, "this_week": 2, "whenever": 3, None: 2}


def collect(repo: str, now: Optional[float] = None) -> Dict[str, Any]:
    prs = gh_json(["pr", "list", "--repo", repo, "--state", "open", "--limit", "50", "--json",
                   "number,title,body,author,url,createdAt,updatedAt,isDraft,additions,deletions,"
                   "files,comments,reviews,mergeable,statusCheckRollup"]) or []
    issues = gh_json(["issue", "list", "--repo", repo, "--state", "open", "--limit", "50", "--json",
                      "number,title,body,author,url,createdAt,updatedAt,labels,comments"]) or []
    rows = [read_pr(p, now) for p in prs] + [read_issue(i, now) for i in issues]
    ranking = rank(rows)
    rows.sort(key=lambda r: (ORDER.get(r.get("urgency"), 2), -r["quiet_days"]))
    return {"repo": repo, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now or time.time())),
            "open_prs": len(prs), "open_issues": len(issues), "jev": ranking,
            "needs_a_person": [r for r in rows if r["needs"]], "items": rows}


def draft_reply(row: Dict[str, Any]) -> str:
    """A starting point for the maintainer to edit. Never posted by this script."""
    if row["urgent_word"]:
        return (f"Thank you for reporting this. I am reading it now. If this is a security or privacy "
                f"problem, please let's move it to a private report (SECURITY.md) rather than continue here, "
                f"and I will credit you in the fix.")
    if row["kind"] == "pr" and row["checks"]["state"] == "failing":
        return ("Thank you for this. CI is red on it — I will paste which test below. That is often "
                "something environmental rather than your change: the suite runs on Ubuntu and macOS, "
                "3.9 and 3.13, and a test that reads a local keychain or patches a default argument "
                "passes locally and fails there.")
    if row["kind"] == "pr" and row["mergeable"] == "CONFLICTING":
        return ("Thank you for this. It conflicts with main now — if that is because main moved under "
                "you, that is on me, not on you. Happy to rebase it myself if you would rather.")
    if row["kind"] == "pr":
        return ("Thank you for this — reading it properly now. If the idea is right but the mechanism "
                "needs changing, I will say so plainly and credit the idea to you either way.")
    return ("Thank you for raising this. Reading it now; I will come back with either a fix or a "
            "reason it works the way it does.")


def render(report: Dict[str, Any]) -> str:
    lines = [f"# Open PRs and issues — {report['repo']}", "",
             f"{report['open_prs']} open PR(s), {report['open_issues']} open issue(s), "
             f"{len(report['needs_a_person'])} needing a person. {report['at']}"]
    jev = report["jev"]
    if jev.get("status") != "ok":
        lines.append(f"(urgency not ranked: {jev.get('status')}{'/' + jev['reason'] if jev.get('reason') else ''})")
    if not report["items"]:
        lines += ["", "Nothing open. "]
        return "\n".join(lines) + "\n"
    lines += ["", "| | # | what | who | quiet | state | needs |", "|---|---|---|---|---|---|---|"]
    for row in report["items"]:
        state = row["checks"]["state"] if row["kind"] == "pr" else ",".join(row.get("labels") or []) or "-"
        lines.append(f"| {row.get('urgency') or '-'} | [{row['number']}]({row['url']}) | {row['title'][:48]} | "
                     f"{row['author']} | {row['quiet_days']:.0f}d | {state} | {'; '.join(row['needs']) or '-'} |")
    for row in report["needs_a_person"]:
        lines += ["", f"## #{row['number']} — {row['title'][:70]}", f"{row['url']}", "",
                  "Needs: " + "; ".join(row["needs"]), "", "Draft reply (nothing has been posted):", "",
                  "> " + draft_reply(row).replace("\n", "\n> ")]
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", default=os.environ.get("JEV_GITHUB_REPO", "kerpopule/hermes-jev-skills"))
    parser.add_argument("--json", action="store_true", help="the report as JSON instead of markdown")
    parser.add_argument("--out", help="write the report here as well as to stdout")
    parser.add_argument("--quiet-if-nothing", action="store_true",
                        help="print nothing when no item needs a person (for a timer that mails its output)")
    args = parser.parse_args(argv)

    report = collect(args.repo)
    if args.quiet_if_nothing and not report["needs_a_person"]:
        return 0
    text = json.dumps(report, indent=2) if args.json else render(report)
    if args.out:
        try:
            Path(args.out).expanduser().write_text(text, encoding="utf-8")
        except OSError as error:
            print(f"[triage-github] could not write {args.out}: {error}", file=sys.stderr)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
