#!/usr/bin/env python3
"""
build_data.py — Generate data.json for the Canton Dev Fund money site,
entirely from the canton-dev-fund GitHub repository.

Two sources, both in the repo:
  1. Proposal PRs / markdown files  -> committed amount, org, status, dates.
  2. Milestone ISSUES               -> disbursements.

Milestone issues are titled like:
    "Token Standard V2 #97 - Milestone 4: ..."
so the parent proposal is the "#NN" in the title. Inside each milestone
issue's timeline, a payout is recorded as a comment:
    "Paid via https://lighthouse.cantonloop.com/dev-fund/grant/<hash>"
which gives us the disbursed amount (the milestone's funding), the payout
date (the comment date), and a real on-chain Lighthouse link.

The GitVote bot also posts a machine-readable tally we capture for the
decisions view (in-favour %, threshold, pass/fail).

Run:
    GITHUB_TOKEN=<token> python3 build_data.py            # writes ../data.json
    GITHUB_TOKEN=<token> python3 build_data.py --limit 5  # quick validation

A token is optional but strongly recommended (unauthenticated is 60 req/hr;
authenticated is 5000 req/hr). In GitHub Actions the built-in GITHUB_TOKEN
is passed automatically.
"""

import os
import re
import sys
import json
import time
import argparse
import urllib.request
import urllib.error
from collections import defaultdict

REPO = "canton-foundation/canton-dev-fund"
API = "https://api.github.com"

# Org short-code -> canonical display name. Extend as new grantees appear.
ORG_CANON = {
    "DA": "Digital Asset",
    "FCS": "Finoa Consensus Services",
    "IEU": "IntellectEU",
}

# ---- HTTP helpers ------------------------------------------------------------


def gh(path, params=None):
    """GET a GitHub API path, following pagination, returning parsed JSON.
    Retries once on secondary-rate-limit, and fails loudly on hard errors."""
    url = API + path
    if params:
        q = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        url += ("&" if "?" in url else "?") + q
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "canton-devfund-site-builder",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        headers["Authorization"] = "Bearer " + tok

    for attempt in range(3):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode()), r.headers
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt < 2:
                # rate limited — wait for reset if header present, else back off
                reset = e.headers.get("X-RateLimit-Reset")
                wait = max(5, int(reset) - int(time.time())) if reset else 15 * (attempt + 1)
                sys.stderr.write(f"  rate limited, waiting {wait}s...\n")
                time.sleep(min(wait, 90))
                continue
            raise
    raise RuntimeError("giving up after retries: " + url)


def gh_paged(path, params=None, cap=None):
    """Yield every item across all pages of a list endpoint."""
    params = dict(params or {})
    params.setdefault("per_page", 100)
    page = 1
    seen = 0
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


# ---- Parsing helpers ---------------------------------------------------------


def canon_org(org):
    org = (org or "").strip()
    return ORG_CANON.get(org, org)


def parse_cc(text):
    """Pull a CC amount out of free text, e.g. '1,200,000 CC' -> 1200000.0.
    Returns 0.0 when there's no amount, or when the matched number is empty
    or malformed (e.g. a stray 'CC' with no digits in front of it)."""
    if not text:
        return 0.0
    # Require at least one digit so we never capture an empty/comma-only string.
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(?:CC|Canton\s*Coin)", text, re.I)
    if not m:
        return 0.0
    num = m.group(1).replace(",", "").strip()
    try:
        return float(num)
    except ValueError:
        # Malformed amount (e.g. "1,,000" or trailing junk) — treat as unknown
        # rather than crashing the whole build.
        return 0.0


def parse_field(body, name):
    """Extract a metadata field from a proposal body across the repo's
    three formats: pipe table, bold-inline, and bold-bullet."""
    m = re.search(r"\*\*\s*" + name + r"\s*:\s*\*\*\s*(.+?)(?=\*\*[A-Za-z /&]+:\*\*|\n|$)", body)
    if m:
        return m.group(1).strip()
    m = re.search(r"\|\s*" + name + r"\s*\|\s*(.+?)\s*\|", body, re.I)
    return m.group(1).strip() if m else ""


def clean(s):
    s = re.sub(r"\(.*?\)", "", s or "")
    s = re.sub(r"<br\s*/?>", "", s)
    s = re.sub(r"[\[\]]", "", s)
    return re.sub(r"\s+", " ", s).strip(" -*")


# The payout line. "Paid via" is the standard phrasing; we stay tolerant of
# "Payment sent via", "Paid:", "sent via", trailing punctuation, markdown links.
PAID_RE = re.compile(
    r"(?:paid|payment\s+sent|sent|disbursed)\s*(?:via|:)?\s*.*?(https://lighthouse\.cantonloop\.com/\S+)",
    re.I | re.S,
)
# Fallback: any Lighthouse link in a comment that signals a payment.
LH_RE = re.compile(r"https://lighthouse\.cantonloop\.com/\S+", re.I)
PAY_WORDS = re.compile(r"\b(paid|payment|sent|disburse)", re.I)

# Milestone-issue title: "... #97 - Milestone 4: ..."  -> parent PR 97, milestone 4
TITLE_RE = re.compile(r"#(\d+)\s*[-–]\s*Milestone\s*(\d+)", re.I)

# GitVote tally line, e.g.:
#   "So far `60.00%` of the users with binding vote are in favor and `0.00%` are against"
# Backticks around the percentages are optional; matcher tolerates them.
VOTE_RE = re.compile(
    r"`?([\d.]+)%`?\s+of\s+the\s+users.*?in\s+favor.*?`?([\d.]+)%`?\s+are\s+against",
    re.I | re.S,
)
THRESH_RE = re.compile(r"passing threshold:?\s*`?([\d.]+)%", re.I)


def extract_payout(body):
    """Return a Lighthouse URL if this comment records a payout, else None."""
    if not body:
        return None
    m = PAID_RE.search(body)
    if m:
        return m.group(1).rstrip(").,")
    # tolerant fallback: a payment word somewhere + a Lighthouse link somewhere
    if PAY_WORDS.search(body):
        m2 = LH_RE.search(body)
        if m2:
            return m2.group(0).rstrip(").,")
    return None


# ---- Main build --------------------------------------------------------------


def build(limit=None):
    # 1) PROPOSAL PRs -> committed amounts + metadata, keyed by PR number.
    sys.stderr.write("Fetching proposal PRs...\n")
    proposals = {}
    for pr in gh_paged(f"/repos/{REPO}/pulls", {"state": "all"}, cap=limit):
        num = pr["number"]
        body = pr.get("body") or ""
        title = pr.get("title") or ""
        org = canon_org(clean(parse_field(body, "Org")) or clean(parse_field(body, "Organization"))
                        or clean(parse_field(body, "Author")))
        proposals[num] = {
            "pr": num,
            "org": org or "(unspecified)",
            "name": title,
            "approved": (pr.get("merged_at") or "")[:10],
            "status": "Approved" if pr.get("merged_at") else pr.get("state", ""),
            "committed": parse_cc(body),
            "duration": clean(parse_field(body, "Project Duration") or parse_field(body, "Duration")),
            "url": pr.get("html_url"),
            "milestones": [],
            "tx": [],
        }
    sys.stderr.write(f"  {len(proposals)} PRs\n")

    # 2) MILESTONE ISSUES -> disbursements + votes, linked by title "#NN".
    sys.stderr.write("Fetching milestone issues + timelines...\n")
    n_pay = 0
    for issue in gh_paged(f"/repos/{REPO}/issues", {"state": "all"}, cap=limit):
        if "pull_request" in issue:  # issues endpoint also returns PRs; skip them
            continue
        title = issue.get("title") or ""
        tm = TITLE_RE.search(title)
        if not tm:
            continue
        parent_pr = int(tm.group(1))
        ms_num = int(tm.group(2))
        prop = proposals.get(parent_pr)
        if prop is None:
            continue

        ms = {"n": ms_num, "issue": issue["number"], "title": title,
              "url": issue.get("html_url"), "state": issue.get("state", ""),
              "amount": parse_cc(issue.get("body") or ""),
              "vote": None, "paid": False}

        # Walk the issue's comment timeline for votes + payouts.
        for c in gh_paged(f"/repos/{REPO}/issues/{issue['number']}/comments"):
            body = c.get("body") or ""
            # GitVote tally
            vm = VOTE_RE.search(body)
            if vm:
                th = THRESH_RE.search(body)
                ms["vote"] = {
                    "favor": float(vm.group(1)),
                    "against": float(vm.group(2)),
                    "threshold": float(th.group(1)) if th else None,
                    "passed": (float(vm.group(1)) >= (float(th.group(1)) if th else 51)),
                }
            # Payout
            url = extract_payout(body)
            if url:
                amt = ms["amount"] or parse_cc(body)
                tx = {
                    "amt": amt,
                    "date": (c.get("created_at") or "")[:10],
                    "url": url,
                    "src": "lighthouse",
                    # Milestone issue this payment came from, so the site can
                    # link each payment to the thread with its vote + payout.
                    "issue": issue["number"],
                    "issue_url": issue.get("html_url"),
                    "ms": ms_num,
                    "note": f"Paid via Lighthouse — milestone {ms_num}, issue #{issue['number']}",
                }
                prop["tx"].append(tx)
                ms["paid"] = True
                n_pay += 1

        prop["milestones"].append(ms)

    sys.stderr.write(f"  {n_pay} payouts found\n")

    # 3) Roll up per-grant disbursed / remaining / pct, plus has_lighthouse.
    grants = []
    for p in proposals.values():
        if not p["committed"] and not p["tx"]:
            continue  # skip non-grant PRs with nothing to show
        p["tx"].sort(key=lambda t: t["date"] or "")
        p["disbursed"] = sum(t["amt"] for t in p["tx"])
        p["remaining"] = max(p["committed"] - p["disbursed"], 0)
        p["pct"] = round(p["disbursed"] / p["committed"] * 100, 1) if p["committed"] else 0
        p["ntx"] = len(p["tx"])
        p["has_lighthouse"] = any(t["src"] == "lighthouse" for t in p["tx"])
        grants.append(p)

    grants.sort(key=lambda g: -g["committed"])

    # 4) Aggregates for the headline, org chart, monthly chart.
    total_comm = sum(g["committed"] for g in grants)
    total_disb = sum(g["disbursed"] for g in grants)
    org_c, org_d = defaultdict(float), defaultdict(float)
    by_month = defaultdict(float)
    for g in grants:
        org_c[g["org"]] += g["committed"]
        org_d[g["org"]] += g["disbursed"]
        for t in g["tx"]:
            if t["date"]:
                by_month[t["date"][:7]] += t["amt"]

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": f"https://github.com/{REPO}",
        # Repo-derived totals are authoritative here (no external workbook).
        "headline_committed": total_comm,
        "headline_disbursed": total_disb,
        "headline_remaining": total_comm - total_disb,
        "total_disbursed_matched": total_disb,
        "unassigned": 0,  # everything traces to a milestone issue now
        "n_grants": len(grants),
        "n_tx": sum(g["ntx"] for g in grants),
        "n_lighthouse": sum(1 for g in grants for t in g["tx"] if t["src"] == "lighthouse"),
        "by_month": dict(sorted(by_month.items())),
        "org": sorted(
            [{"org": o, "committed": org_c[o], "disbursed": org_d[o]} for o in org_c],
            key=lambda x: -x["committed"],
        ),
        "grants": grants,
    }
    return payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="cap items for quick validation")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "data.json"))
    args = ap.parse_args()

    payload = build(limit=args.limit)
    out = os.path.abspath(args.out)
    with open(out, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    sys.stderr.write(
        f"\nWrote {out}\n"
        f"  grants: {payload['n_grants']}  committed: {payload['headline_committed']:,.0f} CC\n"
        f"  disbursed: {payload['headline_disbursed']:,.0f} CC  "
        f"({payload['n_lighthouse']} Lighthouse-verified payments)\n"
    )


if __name__ == "__main__":
    import urllib.parse  # noqa: E402 (used in gh())
    main()
