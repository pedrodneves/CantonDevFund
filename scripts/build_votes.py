#!/usr/bin/env python3
"""
build_votes.py — Generate votes.json for the Canton Dev Fund votes page.

Walks the canton-dev-fund repo and collects every GitVote vote, from both:
  - proposal PRs  (whole-grant approval votes)
  - milestone issues (milestone completion votes)

Each GitVote comment looks like one of:
  "## Vote status  So far `60.00%` ... in favor and `0.00%` are against
   (passing threshold: `51%`)"   -> vote IN PROGRESS
  "## Vote closed  The vote **did not pass**. `20.00%` ... were in favor ..."
                                  -> vote CLOSED (passed / failed)

We parse the tally, threshold, vote counts, and the parent PR / milestone so
the page can list all votes filterable by status.

Run:
    GITHUB_TOKEN=<token> python3 build_votes.py            # writes ../votes.json
    GITHUB_TOKEN=<token> python3 build_votes.py --limit 5  # quick check
"""

import os
import re
import sys
import json
import time
import argparse
import urllib.request
import urllib.error
import urllib.parse

REPO = "canton-foundation/canton-dev-fund"
API = "https://api.github.com"

# Milestone-issue title -> parent PR + milestone number (e.g. "#97 Milestone 4").
TITLE_RE = re.compile(r"#(\d+)\s*[-–—]?\s*Milestone\s*(\d+)\s*[:\-–]?\s*(.*)?", re.I)


# ---- HTTP -------------------------------------------------------------------


def gh(path, params=None):
    url = API + path
    if params:
        q = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        url += ("&" if "?" in url else "?") + q
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "canton-devfund-votes",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        headers["Authorization"] = "Bearer " + tok
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=45) as r:
                return json.loads(r.read().decode()), r.headers
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt < 2:
                reset = e.headers.get("X-RateLimit-Reset")
                wait = max(5, int(reset) - int(time.time())) if reset else 15 * (attempt + 1)
                sys.stderr.write(f"  rate limited, waiting {min(wait,90)}s...\n")
                time.sleep(min(wait, 90))
                continue
            raise
    raise RuntimeError("giving up: " + url)


def gh_paged(path, params=None, cap=None):
    params = dict(params or {})
    params.setdefault("per_page", 100)
    page, seen = 1, 0
    while True:
        params["page"] = page
        data, _ = gh(path, params)
        if not isinstance(data, list) or not data:
            break
        for item in data:
            yield item
            seen += 1
            if cap and seen >= cap:
                return
        if len(data) < params["per_page"]:
            break
        page += 1


# ---- Vote parsing -----------------------------------------------------------


def parse_vote(body):
    """Parse a GitVote comment into a vote record, or None if not a vote."""
    if not body:
        return None
    is_closed = "Vote closed" in body
    is_status = "Vote status" in body
    if not (is_closed or is_status):
        return None

    fav = re.search(r"`?([\d.]+)%`?\s+of the users.*?(?:are|were)\s+in favor", body, re.S | re.I)
    agn = re.search(r"([\d.]+)%`?\s+(?:are|were)\s+against", body, re.I)
    thr = re.search(r"passing threshold:?\s*`?([\d.]+)%", body, re.I)
    favor = float(fav.group(1)) if fav else 0.0
    against = float(agn.group(1)) if agn else 0.0
    threshold = float(thr.group(1)) if thr else 51.0

    # Summary counts row: | In favor | Against | Abstain | Not voted | ... | a | b | c | d |
    counts = None
    sm = re.search(
        r"In favor.*?Against.*?Abstain.*?Not voted.*?\|[\s\-:|]+\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|",
        body, re.S)
    if sm:
        counts = {"favor": int(sm.group(1)), "against": int(sm.group(2)),
                  "abstain": int(sm.group(3)), "not_voted": int(sm.group(4))}

    if is_status:
        status = "in_progress"
    else:
        passed = "did not pass" not in body.lower()
        status = "passed" if passed else "failed"

    return {"status": status, "favor": favor, "against": against,
            "threshold": threshold, "counts": counts}


def latest_vote_in_timeline(issue_number):
    """Scan an issue/PR's comments; return the most recent GitVote record."""
    latest = None
    latest_date = ""
    for c in gh_paged(f"/repos/{REPO}/issues/{issue_number}/comments"):
        v = parse_vote(c.get("body") or "")
        if v:
            d = c.get("created_at") or ""
            if d >= latest_date:  # keep the newest vote comment
                latest, latest_date = v, d
                latest["comment_date"] = d[:10]
    return latest


# ---- Build ------------------------------------------------------------------


def build(limit=None):
    votes = []

    # 1) Milestone-issue votes (and any other issue carrying a GitVote comment).
    sys.stderr.write("Scanning issues for votes...\n")
    for issue in gh_paged(f"/repos/{REPO}/issues", {"state": "all"}, cap=limit):
        if "pull_request" in issue:
            continue
        num = issue["number"]
        title = issue.get("title") or ""
        v = latest_vote_in_timeline(num)
        if not v:
            continue
        tm = TITLE_RE.search(title)
        votes.append({
            "kind": "milestone" if tm else "issue",
            "title": title,
            "number": num,
            "url": issue.get("html_url"),
            "pr": int(tm.group(1)) if tm else None,
            "milestone": int(tm.group(2)) if tm else None,
            "issue_state": issue.get("state"),
            **v,
        })

    # 2) Proposal-PR votes (whole-grant approval votes).
    sys.stderr.write("Scanning PRs for votes...\n")
    for pr in gh_paged(f"/repos/{REPO}/pulls", {"state": "all"}, cap=limit):
        num = pr["number"]
        v = latest_vote_in_timeline(num)
        if not v:
            continue
        votes.append({
            "kind": "proposal",
            "title": pr.get("title") or ("PR #" + str(num)),
            "number": num,
            "url": pr.get("html_url"),
            "pr": num,
            "milestone": None,
            "issue_state": pr.get("state"),
            **v,
        })

    # Sort: in-progress first (most relevant), then by number desc.
    order = {"in_progress": 0, "passed": 1, "failed": 2}
    votes.sort(key=lambda x: (order.get(x["status"], 3), -x["number"]))

    counts = {"in_progress": 0, "passed": 0, "failed": 0}
    for v in votes:
        counts[v["status"]] = counts.get(v["status"], 0) + 1

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": f"https://github.com/{REPO}",
        "n_votes": len(votes),
        "n_in_progress": counts["in_progress"],
        "n_passed": counts["passed"],
        "n_failed": counts["failed"],
        "votes": votes,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "votes.json"))
    args = ap.parse_args()
    payload = build(limit=args.limit)
    with open(os.path.abspath(args.out), "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    sys.stderr.write(
        f"\nWrote votes.json: {payload['n_votes']} votes "
        f"({payload['n_in_progress']} in progress, {payload['n_passed']} passed, "
        f"{payload['n_failed']} failed).\n")


if __name__ == "__main__":
    main()
