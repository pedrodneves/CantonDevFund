/* =============================================================
   Canton Development Fund — Money page logic
   Fetches data.json at load, then renders the headline, the
   concentration chart, the monthly chart, and the per-grant
   ledger with expandable payment history.
   Update the site by regenerating data.json — no code changes.
   ============================================================= */

// Base repo URL, used to build PR / Files links.
const REPO = "https://github.com/canton-foundation/canton-dev-fund";

// Holds the loaded dataset and the current ledger sort key.
let DATA = null;
let sortBy = "committed";

/* ---- Formatting helpers ------------------------------------ */

// Compact number: 1.2M / 350k / 900.
function fmt(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(0) + "k";
  return "" + Math.round(n);
}

// Full number with thousands separators (e.g. 12,000,000).
function fmtc(n) {
  return Math.round(n).toLocaleString();
}

/* ---- Boot ------------------------------------------------- */

// Fetch the data file, then paint every section. Fails loudly
// (a visible message) rather than silently showing an empty page.
async function boot() {
  const root = document.getElementById("ledger");
  try {
    const res = await fetch("data.json", { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    DATA = await res.json();
  } catch (err) {
    root.innerHTML =
      '<div class="state err">Could not load data.json (' +
      err.message +
      "). Make sure it sits next to index.html.</div>";
    return;
  }
  renderHeadline();
  renderMetrics();
  renderReconciliation();
  renderConcentration();
  renderMonths();
  wireControls();
  renderLedger();
}

/* ---- Headline: committed vs disbursed --------------------- */

function renderHeadline() {
  const pct = (DATA.headline_disbursed / DATA.headline_committed) * 100;

  const fill = document.getElementById("prog-fill");
  fill.style.width = Math.max(pct, 7) + "%"; // floor so the label stays readable
  fill.textContent = fmt(DATA.headline_disbursed) + " CC";

  document.getElementById("prog-pct").textContent =
    pct.toFixed(1) + "% of " + fmt(DATA.headline_committed) + " committed";
  document.getElementById("disb-lab").textContent =
    "Disbursed " + fmtc(DATA.headline_disbursed) + " CC";
  document.getElementById("comm-lab").textContent =
    "Committed " + fmtc(DATA.headline_committed) + " CC";
  document.getElementById("h-disb").textContent = fmt(DATA.headline_disbursed);
  document.getElementById("h-rem").textContent = fmt(DATA.headline_remaining);
}

/* ---- Four top-line metrics -------------------------------- */

function renderMetrics() {
  const pct = (DATA.headline_disbursed / DATA.headline_committed) * 100;
  document.getElementById("m-comm").textContent = fmt(DATA.headline_committed);
  document.getElementById("m-pct").textContent = pct.toFixed(0) + "%";
  document.getElementById("m-grants").textContent = DATA.n_grants;
  document.getElementById("m-tx").textContent = DATA.n_tx;
}

/* ---- Reconciliation note ---------------------------------- */

// Explains the gap between the headline disbursed total and the
// sum of per-grant disbursements (payments confirmed on-chain but
// not yet tied to a specific grant). Shown honestly, not hidden.
function renderReconciliation() {
  const el = document.getElementById("recon");
  if (DATA.unassigned > 0) {
    el.innerHTML =
      "<b>Reconciliation:</b> " +
      fmtc(DATA.headline_disbursed) +
      " CC disbursed in total — " +
      fmtc(DATA.total_disbursed_matched) +
      " CC matched to the grants below, and " +
      fmtc(DATA.unassigned) +
      " CC Lighthouse-confirmed on-chain but not yet tied to a grant (payments awaiting a verifiable mint date). Shown separately rather than forced to match.";
  } else {
    el.innerHTML =
      "<b>Reconciliation:</b> per-grant disbursements sum exactly to the headline total.";
  }
}

/* ---- Concentration: committed vs disbursed by org --------- */

function renderConcentration() {
  const maxc = Math.max.apply(null, DATA.org.map((o) => o.committed));
  document.getElementById("conc").innerHTML = DATA.org
    .filter((o) => o.committed > 0)
    .map(
      (o) =>
        '<div class="row"><div class="org" title="' +
        o.org +
        '">' +
        o.org +
        "</div>" +
        '<div class="bars">' +
        '<div class="track"><div class="fc" style="width:' +
        ((o.committed / maxc) * 100).toFixed(1) +
        '%"></div></div>' +
        '<div class="track"><div class="fd" style="width:' +
        ((o.disbursed / maxc) * 100).toFixed(1) +
        '%"></div></div>' +
        "</div>" +
        '<div class="amt"><span class="c">' +
        fmt(o.committed) +
        '</span> · <span class="d">' +
        fmt(o.disbursed) +
        "</span></div></div>"
    )
    .join("");
}

/* ---- Monthly disbursement chart --------------------------- */

function renderMonths() {
  const mo = Object.entries(DATA.by_month);
  const mmax = Math.max.apply(null, mo.map(([, v]) => v).concat([1]));
  document.getElementById("months").innerHTML = mo
    .map(
      ([m, v]) =>
        '<div class="col"><div class="bar" style="height:' +
        ((v / mmax) * 100).toFixed(0) +
        '%"><span class="val">' +
        fmt(v) +
        '</span></div><div class="lab">' +
        m +
        "</div></div>"
    )
    .join("");
}

/* ---- Ledger controls (search + sort) ---------------------- */

function wireControls() {
  document.getElementById("search").oninput = renderLedger;
  document.getElementById("sort").onchange = (e) => {
    sortBy = e.target.value;
    renderLedger();
  };
}

/* ---- Per-grant ledger ------------------------------------- */

// Build the two rows for one grant: a clickable summary row and a
// hidden detail row holding links + full payment history.
// Build the "Milestone issues" block for a grant's detail dropdown: one
// linked chip per milestone issue (open or closed), in milestone order.
// Each chip shows the milestone number, a paid/open state, and links to the
// issue thread where the vote and "Paid via" payout live.
function milestoneLinks(g) {
  if (!g.milestones || !g.milestones.length) return "";
  const chips = g.milestones
    .slice()
    .sort((a, b) => (a.n || 0) - (b.n || 0))
    .map((m) => {
      // A milestone is "paid" if a payout was found; otherwise show its
      // issue state (open/closed) so context is clear either way.
      const tag = m.paid ? "paid" : m.state === "closed" ? "closed" : "open";
      // Month distinguishes recurring milestones (e.g. monthly maintenance),
      // shown between the number and the issue: "Milestone 1 · December · #652".
      const monthPart = m.month ? " · " + m.month : "";
      return (
        '<a class="mslink ' +
        tag +
        '" href="' +
        m.url +
        '" target="_blank" rel="noopener" title="' +
        (m.label || m.title || "").replace(/"/g, "") +
        '">Milestone ' +
        m.n +
        monthPart +
        ' · #' +
        m.issue +
        ' <span class="mstag">' +
        tag +
        "</span></a>"
      );
    })
    .join("");
  return (
    '<div class="dtitle">Milestone issues · ' +
    g.milestones.length +
    "</div><div class=\"mslinks\">" +
    chips +
    "</div>"
  );
}

function rowFor(g, i) {
  const pct = g.committed ? (g.disbursed / g.committed) * 100 : 0;

  // Payment history block, or a "none yet" note.
  let history;
  if (g.tx.length) {
    const lhCount = g.tx.filter((t) => t.src === "lighthouse").length;
    const verified = g.has_lighthouse
      ? ' · <span class="lh-verified">✦ ' +
        lhCount +
        " Lighthouse-verified on-chain</span>"
      : "";
    const rows = g.tx
      .map((t) => {
        // Real Lighthouse explorer link where we have one; otherwise
        // the GitHub PR the payment was recorded against. Never a
        // fabricated hash.
        let link;
        if (t.src === "lighthouse") {
          const note = (t.note || "").replace(/"/g, "");
          link =
            '<a class="txsrc lh" href="' +
            t.url +
            '" target="_blank" rel="noopener" title="' +
            note +
            '">Lighthouse ↗</a>';
        } else if (t.src === "issue") {
          // Payment recorded via the milestone issue (no separate Lighthouse
          // link). The issue link on the right already covers the source, so
          // we don't add a redundant second link here.
          link = "";
        } else if (t.url) {
          link =
            '<a class="txsrc gh" href="' +
            t.url +
            '" target="_blank" rel="noopener">GitHub PR ↗</a>';
        } else {
          link = '<span class="txsrc none">—</span>';
        }
        // Link to the milestone issue this payment came from, when known —
        // gives the payment its context (vote thread + "Paid via" comment).
        // Labelled with the milestone number when we have it.
        const issueLabel = t.ms ? "M" + t.ms + " · #" + t.issue : "Issue #" + t.issue;
        const issueTitle = t.label || "Milestone issue #" + t.issue;
        const issueLink = t.issue
          ? '<a class="txsrc issue" href="' +
            (t.issue_url ||
              "https://github.com/canton-foundation/canton-dev-fund/issues/" +
                t.issue) +
            '" target="_blank" rel="noopener" title="' +
            issueTitle.replace(/"/g, "") +
            '">' +
            issueLabel +
            " ↗</a>"
          : "";
        // The milestone's descriptive title, shown between date and amount so
        // each payment reads as "what it was for", not just a number.
        const msLabel = t.label
          ? '<span class="txlabel" title="' +
            t.label.replace(/"/g, "") +
            '">' +
            t.label +
            "</span>"
          : '<span class="txlabel"></span>';
        return (
          '<div class="txrow"><span class="date">' +
          (t.date || "date pending") +
          "</span>" +
          msLabel +
          '<span class="amt">' +
          fmtc(t.amt) +
          " CC</span>" +
          '<span class="txlinks">' +
          issueLink +
          link +
          "</span>" +
          "</div>"
        );
      })
      .join("");
    history =
      '<div class="dtitle">Payment history · ' +
      g.ntx +
      " payment" +
      (g.ntx > 1 ? "s" : "") +
      verified +
      "</div>" +
      rows;
  } else {
    history = '<div class="noTx">No disbursements recorded yet.</div>';
  }

  return (
    // Summary row
    '<tr class="grow" data-i="' +
    i +
    '">' +
    '<td><span class="caret">▸</span><span class="org">' +
    g.org +
    '</span><div class="nm" title="' +
    g.name +
    '">' +
    g.name +
    "</div></td>" +
    '<td><a class="pr-link" href="' +
    g.url +
    '" target="_blank" rel="noopener" onclick="event.stopPropagation()">#' +
    g.pr +
    " ↗</a></td>" +
    '<td class="nm">' +
    (g.approved || "—") +
    "</td>" +
    '<td class="num c">' +
    fmtc(g.committed) +
    "</td>" +
    '<td class="num d">' +
    (g.disbursed ? fmtc(g.disbursed) : "—") +
    "</td>" +
    '<td class="num r">' +
    fmtc(g.remaining) +
    "</td>" +
    '<td class="pctcell"><span class="lbl">' +
    pct.toFixed(0) +
    '% paid</span><div class="minibar"><div class="f" style="width:' +
    pct.toFixed(0) +
    '%"></div></div></td>' +
    "</tr>" +
    // Detail row
    '<tr class="detailrow" data-i="' +
    i +
    '"><td colspan="7"><div class="detailwrap">' +
    (g.approved || g.duration
      ? '<div class="dmeta">' +
        (g.approved ? "<span>Approved <b>" + g.approved + "</b></span>" : "") +
        (g.duration ? "<span>Term <b>" + g.duration + "</b></span>" : "") +
        "</div>"
      : "") +
    '<div class="links">' +
    '<a class="linkbtn primary" href="' +
    g.url +
    '" target="_blank" rel="noopener">Proposal PR #' +
    g.pr +
    " ↗</a>" +
    '<a class="linkbtn" href="' +
    REPO +
    "/pull/" +
    g.pr +
    '/files" target="_blank" rel="noopener">Files</a>' +
    "</div>" +
    milestoneLinks(g) +
    history +
    "</div></td></tr>"
  );
}

function renderLedger() {
  const q = document.getElementById("search").value.toLowerCase();

  // Filter by search text across org + proposal name.
  let rows = DATA.grants.filter(
    (g) => !q || (g.org + " " + g.name).toLowerCase().includes(q)
  );

  // Sort by the selected key (approval sorts by date string; the rest numeric).
  rows.sort((a, b) => {
    if (sortBy === "approved")
      return (b.approved || "").localeCompare(a.approved || "");
    return (b[sortBy] || 0) - (a[sortBy] || 0);
  });

  document.getElementById("ledger").innerHTML =
    '<table class="ltable"><thead><tr>' +
    "<th>Organization / proposal</th><th>PR</th><th>Approved</th>" +
    '<th class="r">Committed</th><th class="r">Disbursed</th><th class="r">Remaining</th><th>Paid</th>' +
    "</tr></thead><tbody>" +
    rows.map((g, i) => rowFor(g, i)).join("") +
    "</tbody></table>";

  // Wire up expand/collapse on each summary row.
  document.querySelectorAll(".grow").forEach((row) => {
    row.onclick = () => {
      const i = row.dataset.i;
      const det = document.querySelector('.detailrow[data-i="' + i + '"]');
      det.classList.toggle("open");
      row.querySelector(".caret").textContent = det.classList.contains("open")
        ? "▾"
        : "▸";
    };
  });
}

// Kick everything off once the DOM is ready.
document.addEventListener("DOMContentLoaded", boot);
