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
import base64
import argparse
import urllib.request
import urllib.error
import urllib.parse
from collections import defaultdict

REPO = "canton-foundation/canton-dev-fund"
API = "https://api.github.com"

# Lighthouse is the authoritative, live on-chain disbursement source. Its
# events feed lists every grant-reward mint, with a `reason` string that ties
# each payment to a PR and issue in the repo.
#
# NOTE: set this to the exact endpoint the dev-fund page fetches. From the
# browser Network tab it is the "grants" request that returns {"events":[...]}.
# It is cursor-paginated via pagination.next_cursor_id. If the path is wrong
# the build fails loudly (0 disbursements) rather than committing bad data.
LIGHTHOUSE_EVENTS = "https://lighthouse.cantonloop.com/api/dev-fund/grants"

# Parsers for the Lighthouse `reason` string, e.g.
#   "DA Token Standard V2 PR97 - Milestone 6: ... Issue436 https://.../issues/436"
# Formats vary ("PR97", "PR 407", "Pr 50"), so the matchers stay tolerant.
LH_PR_RE = re.compile(r"\bPR\s*#?\s*(\d+)", re.I)
LH_ISSUE_URL_RE = re.compile(r"/issues/(\d+)")
LH_ISSUE_RE = re.compile(r"\bIssue\s*#?\s*(\d+)", re.I)
LH_MS_RE = re.compile(r"Milestone\s*(\d+)", re.I)

# Org short-code -> canonical display name. Extend as new grantees appear.
ORG_CANON = {
    "DA": "Digital Asset",
    "FCS": "Finoa Consensus Services",
    "IEU": "IntellectEU",
}

# Per-PR overrides for grants whose proposal file parses wrong or is missing a
# field (org, name, or committed amount). Add a line here to correct any grant.
# `committed` here wins over the parsed proposal amount.
PR_OVERRIDES = {
    105: {"org": "Moonsong Labs", "name": "Git-Based DAR Dependencies for dpm"},
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


# ---- Lighthouse (on-chain disbursements) ------------------------------------


def fetch_lighthouse_events(limit=None):
    """Page through the Lighthouse events feed and return the raw event list.
    The feed is cursor-paginated (pagination.has_next / next_cursor_id)."""
    events = []
    cursor = None
    pages = 0
    seen_ids = set()
    while True:
        url = LIGHTHOUSE_EVENTS + "?limit=100"
        if cursor:
            # The cursor query-param name isn't documented; "cursor_id" matches
            # the response's "next_cursor_id" field. If pagination ever silently
            # fails, the dedupe below stops us rather than looping forever.
            url += f"&cursor_id={cursor}"
        req = urllib.request.Request(url, headers={"User-Agent": "canton-devfund-site"})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                data = json.loads(r.read().decode())
        except Exception as e:
            sys.stderr.write(f"  Lighthouse fetch failed on page {pages + 1}: {e}\n")
            break
        batch = data.get("events", [])
        # Stop if this page repeats events we've already collected (cursor param
        # name wrong, or we've wrapped around) — avoids an infinite loop.
        new = [e for e in batch if e.get("event_id") not in seen_ids]
        if not new:
            break
        for e in new:
            seen_ids.add(e.get("event_id"))
        events.extend(new)
        pages += 1
        pg = data.get("pagination", {})
        if not pg.get("has_next"):
            break
        cursor = pg.get("next_cursor_id")
        if not cursor:
            break
        if limit and len(events) >= limit:
            break
        if pages > 300:  # safety cap
            break
    sys.stderr.write(f"  Lighthouse: {len(events)} events over {pages} pages.\n")
    return events


def parse_lighthouse_payment(ev):
    """Turn one Lighthouse event into a normalized payment, or None if it's a
    test mint, an expired/non-mint event, or unparseable.

    Returns: {pr, issue, ms, amt, date, url} where amt is the EXACT on-chain
    amount (we show the real figure, e.g. 1,199,990 — not rounded)."""
    reason = ev.get("reason", "") or ""
    status = (ev.get("status") or "").lower()

    # Only real, completed mints — skip test mints, expired, and tiny amounts.
    if status != "minted":
        return None
    if "test for" in reason.lower():
        return None
    try:
        amt = float(ev.get("amount", 0))
    except (TypeError, ValueError):
        return None
    if amt <= 100:  # 10 CC test-mint noise
        return None

    prm = LH_PR_RE.search(reason)
    if not prm:
        return None
    pr = int(prm.group(1))

    # Prefer the explicit /issues/NN URL; fall back to "Issue NN".
    im = LH_ISSUE_URL_RE.search(reason) or LH_ISSUE_RE.search(reason)
    issue = int(im.group(1)) if im else None
    msm = LH_MS_RE.search(reason)
    ms = int(msm.group(1)) if msm else None

    return {
        "pr": pr,
        "issue": issue,
        "ms": ms,
        "amt": amt,                              # exact on-chain amount
        "date": (ev.get("event_time") or "")[:10],
        "url": (f"https://github.com/{REPO}/issues/{issue}" if issue else None),
    }


# ---- Parsing helpers ---------------------------------------------------------


def canon_org(org):
    org = (org or "").strip()
    return ORG_CANON.get(org, org)


def _proposal_title(body, filename):
    """Human-readable proposal title. The filename is the most reliable source
    (e.g. '2026-06-IEU-Daml Code Assistant.md' -> 'Daml Code Assistant'); we
    strip the date prefix and any leading org short-code segment."""
    name = re.sub(r"\.md$", "", filename)
    name = re.sub(r"^\d{4}-\d{2}-", "", name)          # drop date prefix
    # Drop a leading "ORG-" segment (e.g. "IEU-", "DA-") if present.
    name = re.sub(r"^[A-Za-z]{2,12}-", "", name)
    name = name.replace("-", " ").replace("_", " ").strip()
    return name[:80] if name else filename


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
        return 0.0


def parse_field(body, name):
    """Extract a metadata field from a proposal body across the repo's
    three formats: pipe table, bold-inline, and bold-bullet."""
    m = re.search(r"\*\*\s*" + name + r"\s*:\s*\*\*\s*(.+?)(?=\*\*[A-Za-z /&]+:\*\*|\n|$)", body)
    if m:
        return m.group(1).strip()
    # Pipe table row: match the field as a whole cell (anchored between pipes)
    # so "Approved" doesn't match inside another cell's text, and capture only
    # up to the next pipe.
    m = re.search(r"(?:^|\n)\|\s*" + name + r"\s*\|\s*([^|\n]+?)\s*\|", body, re.I)
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

# Milestone-issue title. Real examples:
#   "ISS-Based BFT #53 Milestone 1: Core Primitives – Mempool..."
#   "Token Standard V2 #97 - Milestone 4: Performance-Optimized Core"
# The parent PR number comes just before "Milestone" (dash optional), the
# milestone number after it, and an optional ": descriptive title" follows.
TITLE_RE = re.compile(
    r"#(\d+)\s*[-–—]?\s*Milestone\s*(\d+)\s*[:\-–]?\s*(.*)?",
    re.I,
)

# GitVote tally line, e.g.:
#   "So far `60.00%` of the users with binding vote are in favor and `0.00%` are against"
# Backticks around the percentages are optional; matcher tolerates them.
VOTE_RE = re.compile(
    r"`?([\d.]+)%`?\s+of\s+the\s+users.*?in\s+favor.*?`?([\d.]+)%`?\s+are\s+against",
    re.I | re.S,
)
THRESH_RE = re.compile(r"passing threshold:?\s*`?([\d.]+)%", re.I)


# Milestone amount line in a PR body, e.g.:
#   "Milestone 1 ... 1,500,000 CC"
#   "| Milestone 1 | Core Primitives | 1,500,000 CC |"  (across table cells)
# Maps milestone number -> amount, used when the milestone issue itself
# doesn't restate the figure. Allows table pipes between the label and amount.
MS_AMOUNT_RE = re.compile(
    r"(?:Milestone|M)\s*(\d+)\b[^\n]*?(\d[\d,]{3,}(?:\.\d+)?)\s*(?:CC|Canton\s*Coin)",
    re.I,
)


def parse_milestone_amounts(body):
    """Return {milestone_number: amount} parsed from a PR body's schedule."""
    out = {}
    if not body:
        return out
    for m in MS_AMOUNT_RE.finditer(body):
        n = int(m.group(1))
        try:
            amt = float(m.group(2).replace(",", ""))
        except ValueError:
            continue
        # Keep the first (or largest) amount seen for a milestone number.
        if n not in out or amt > out[n]:
            out[n] = amt
    return out


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


def parse_committed(body):
    """Total committed CC for a proposal, from its funding line.
    Tries labeled funding lines (several phrasings), then a number on the line
    after a bare label, then a sum of milestone amounts. Returns (amount,
    per_month) — per_month flags a recurring monthly figure."""
    # Labeled line — the alternation is wrapped so group 1 is always the number.
    label = (r"(?:(?:Total\s+)?Funding\s+Request(?:ed)?|Grant\s+of|Grant\s+Amount|"
             r"Total\s+Grant|Amount\s+Requested)")
    m = re.search(label + r"\s*:?\**\s*[:\-]?\s*\**\s*([\d,]+(?:\.\d+)?)\s*(?:CC|Canton\s*Coin)",
                  body, re.I)
    if m:
        amt = float(m.group(1).replace(",", ""))
        tail = body[m.end():m.end() + 30].lower()
        per_month = "per month" in tail or "/month" in tail or "/ month" in tail
        return amt, per_month
    # Number on the line after a bare "Total Funding Request:" label.
    m = re.search(r"Total\s+Funding\s+Request\s*:?\**\s*\**\s*\n+\s*[*\-\s]*([\d,]{6,})", body, re.I)
    if m:
        return float(m.group(1).replace(",", "")), False
    # Sum of milestone amounts from a table.
    ms = re.findall(r"(?:^|\n)[|\s]*(?:Milestone\s*\d+|M\d+)\b[^\n]*?([\d,]{5,})\s*CC", body, re.I)
    if ms:
        total = sum(float(x.replace(",", "")) for x in ms)
        if total >= 10000:
            return total, False
    return None, False


def _proposal_title(body, filename):
    """Human proposal title from the filename (date + org prefix stripped)."""
    name = re.sub(r"\.md$", "", filename)
    name = re.sub(r"^\d{4}-\d{2}-", "", name)
    name = re.sub(r"^[A-Za-z]{2,14}-", "", name)  # drop leading ORG- segment
    return name.replace("-", " ").replace("_", " ").strip()[:80] or filename


def load_proposals(limit=None):
    """Read /proposals/*.md, return {pr: grant_dict} for APPROVED proposals.
    Each grant has: pr, org, name, approved, committed, per_month, url."""
    listing, _ = gh(f"/repos/{REPO}/contents/proposals")
    md = [f for f in listing
          if f.get("name", "").endswith(".md") and "_template" not in f["name"]]
    if limit:
        md = md[:limit]

    grants = {}
    for f in md:
        raw, _ = gh(f"/repos/{REPO}/contents/proposals/{urllib.parse.quote(f['name'])}")
        body = base64.b64decode(raw.get("content", "")).decode("utf-8", "replace")

        # PR number from the "| PR | [#NN](...) |" row or any /pull/NN link.
        prm = re.search(r"\|\s*PR\s*\|\s*\[?#?(\d+)", body, re.I) or re.search(r"/pull/(\d+)", body)
        if not prm:
            continue
        pr = int(prm.group(1))

        status = clean(parse_field(body, "Status"))
        if "approv" not in status.lower():
            continue  # only funded grants

        org = canon_org(clean(parse_field(body, "Org"))
                        or clean(parse_field(body, "Organization"))
                        or clean(parse_field(body, "Author")))
        committed, per_month = parse_committed(body)

        grants[pr] = {
            "pr": pr,
            "org": org or "(unspecified)",
            "name": _proposal_title(body, f["name"]),
            "approved": clean(parse_field(body, "Approved"))[:10],
            "status": "Approved",
            "committed": committed or 0.0,
            "per_month": per_month,
            "duration": clean(parse_field(body, "Project Duration")
                              or parse_field(body, "Duration")),
            "url": f"https://github.com/{REPO}/pull/{pr}",
            "milestones": [],
            "tx": [],
        }
    return grants


def build(limit=None):
    """Build data.json from GitHub + Lighthouse only (no workbook):
      - PROPOSAL FILES (/proposals/*.md) -> committed amount + org + name,
        for every APPROVED grant, keyed by PR number.
      - LIGHTHOUSE -> actual on-chain disbursements (the real payments), live.
      - REPO issues -> milestone names + issue links, to label each payment.
    """
    # 1) APPROVED grants from the proposal files.
    sys.stderr.write("Fetching approved proposals from /proposals ...\n")
    grants_by_pr = load_proposals(limit=limit)
    # Apply any per-PR org/name/committed overrides for odd-format proposals.
    for pr, g in grants_by_pr.items():
        ov = PR_OVERRIDES.get(pr)
        if ov:
            g.update(ov)
    total_committed = sum(g.get("committed", 0) for g in grants_by_pr.values())
    sys.stderr.write(f"  {len(grants_by_pr)} approved grants "
                     f"({total_committed:,.0f} CC committed).\n")

    # 2) DISBURSEMENTS from Lighthouse (authoritative, on-chain, live).
    sys.stderr.write("Fetching disbursements from Lighthouse...\n")
    lh_events = fetch_lighthouse_events(limit=limit)
    if not lh_events:
        # Fail loudly rather than silently zeroing out every disbursement.
        # The workflow's validation step will catch this and keep the old data.
        raise RuntimeError(
            "Lighthouse returned no events — check LIGHTHOUSE_EVENTS URL. "
            "Refusing to build with zero disbursements.")
    lh_payments = defaultdict(list)   # pr -> [payment dicts]
    for ev in lh_events:
        p = parse_lighthouse_payment(ev)
        if p is None:
            continue
        lh_payments[p["pr"]].append(p)
    n_pay = sum(len(v) for v in lh_payments.values())
    sys.stderr.write(f"  {n_pay} real disbursements across {len(lh_payments)} grants.\n")

    # 3) MILESTONE NAMES + issue links from the repo, keyed by issue number, so
    #    each Lighthouse payment (which carries its issue number) can be labeled.
    sys.stderr.write("Fetching milestone names from repo issues...\n")
    ms_by_issue = {}                  # issue_num -> {label, ms_title, n, url, state}
    ms_by_pr = defaultdict(list)      # pr -> [milestone dicts] for the chips
    for issue in gh_paged(f"/repos/{REPO}/issues", {"state": "all"}, cap=limit):
        if "pull_request" in issue:
            continue
        title = issue.get("title") or ""
        tm = TITLE_RE.search(title)
        if not tm:
            continue
        parent_pr = int(tm.group(1))
        if parent_pr not in grants_by_pr:
            continue
        ms_num = int(tm.group(2))
        ms_title = (tm.group(3) or "").strip()
        issue_body = issue.get("body") or ""
        if not ms_title:
            hm = re.search(r"#{1,4}\s*Milestone\s*" + str(ms_num) + r"\s*[:\-–]\s*(.+)",
                           issue_body, re.I)
            if hm:
                ms_title = hm.group(1).strip()

        # Recurring/ongoing milestones (e.g. monthly maintenance) end their
        # title with "- <Month>", which distinguishes otherwise-identical
        # "Milestone 1" issues. Pull that month out so we can show it, and
        # strip it from ms_title so it isn't duplicated.
        month = ""
        mm = re.search(
            r"[-–—]\s*(January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s*$",
            ms_title, re.I)
        if mm:
            month = mm.group(1).capitalize()
            ms_title = ms_title[:mm.start()].strip(" -–—")

        # Build the label: "Milestone N: Title (Month)" — with each part shown
        # only when present.
        if ms_title and month:
            label = f"Milestone {ms_num}: {ms_title} ({month})"
        elif ms_title:
            label = f"Milestone {ms_num}: {ms_title}"
        elif month:
            label = f"Milestone {ms_num} ({month})"
        else:
            label = f"Milestone {ms_num}"

        rec = {
            "n": ms_num,
            "issue": issue["number"],
            "url": issue.get("html_url"),
            "state": issue.get("state", ""),
            "ms_title": ms_title,
            "month": month,
            "label": label,
        }
        ms_by_issue[issue["number"]] = rec
        ms_by_pr[parent_pr].append(rec)

    # 4) Assemble each grant's payments from Lighthouse, labeled with repo names.
    # Safety net: if Lighthouse paid a PR that has no approved proposal file
    # (odd status, or proposal not parsed), still create a minimal grant so the
    # on-chain disbursement is never dropped — committed shows 0 until fixed.
    for pr in lh_payments:
        if pr not in grants_by_pr:
            grants_by_pr[pr] = {
                "pr": pr, "org": "(unspecified)",
                "name": "PR #" + str(pr),
                "approved": "", "status": "Approved",
                "committed": 0.0, "per_month": False, "duration": "",
                "url": f"https://github.com/{REPO}/pull/{pr}",
                "milestones": [], "tx": [],
            }
            ov = PR_OVERRIDES.get(pr)
            if ov:
                grants_by_pr[pr].update(ov)

    for pr, g in grants_by_pr.items():
        # Milestone chips for the dropdown (from repo).
        g["milestones"] = sorted(ms_by_pr.get(pr, []), key=lambda m: m["n"])
        txs = []
        for p in sorted(lh_payments.get(pr, []), key=lambda x: x["date"]):
            ms = ms_by_issue.get(p["issue"]) if p["issue"] else None
            txs.append({
                "amt": p["amt"],                 # exact on-chain amount
                "date": p["date"],
                "url": p["url"],                 # links to the issue
                "src": "lighthouse",             # all payments are on-chain now
                "issue": p["issue"],
                "issue_url": p["url"],
                "ms": p["ms"] or (ms["n"] if ms else None),
                "ms_title": ms["ms_title"] if ms else "",
                "label": (ms["label"] if ms
                          else (f"Milestone {p['ms']}" if p["ms"] else "On-chain payment")),
            })
        g["tx"] = txs
        g["ntx"] = len(txs)
        g["disbursed"] = sum(t["amt"] for t in txs)
        g["remaining"] = max(g.get("committed", 0) - g["disbursed"], 0)
        g["pct"] = round(g["disbursed"] / g["committed"] * 100, 1) if g.get("committed") else 0
        g["has_lighthouse"] = bool(txs)

    # 5) Recompute aggregates from the live disbursement data.
    grants = sorted(grants_by_pr.values(), key=lambda g: -g.get("committed", 0))
    total_comm = sum(g.get("committed", 0) for g in grants)
    total_disb = sum(g.get("disbursed", 0) for g in grants)
    org_c, org_d = defaultdict(float), defaultdict(float)
    by_month = defaultdict(float)
    for g in grants:
        org_c[g["org"]] += g.get("committed", 0)
        org_d[g["org"]] += g.get("disbursed", 0)
        for t in g.get("tx", []):
            if t["date"]:
                by_month[t["date"][:7]] += t["amt"]

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": f"https://github.com/{REPO}",
        "disbursement_source": "https://lighthouse.cantonloop.com/dev-fund",
        "headline_committed": total_comm,          # from proposal files (GitHub)
        "headline_disbursed": total_disb,          # from Lighthouse (live)
        "headline_remaining": total_comm - total_disb,
        "total_disbursed_matched": total_disb,
        "unassigned": 0,
        "n_grants": len(grants),
        "n_tx": sum(len(g.get("tx", [])) for g in grants),
        "n_lighthouse": sum(len(g.get("tx", [])) for g in grants),  # all on-chain
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
