/* =============================================================
   Votes tab — renders GitVote decisions on the Development Fund.
   Loaded lazily: loadVotes() runs the first time the Votes tab
   is opened, fetches votes.json, and renders filterable cards.
   ============================================================= */

(function () {
  let DATA = null;
  let statusFilter = "all";
  let kindFilter = "all";
  let wired = false;

  const STATUS = {
    in_progress: { label: "In progress", cls: "s-progress" },
    passed: { label: "Passed", cls: "s-passed" },
    failed: { label: "Did not pass", cls: "s-failed" },
  };
  const KIND = { proposal: "Proposal", milestone: "Milestone", issue: "Vote" };

  function escapeHtml(s) {
    return (s || "").replace(/[&<>"]/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch];
    });
  }

  async function loadVotes() {
    const root = document.getElementById("votes");
    try {
      const res = await fetch("votes.json", { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      DATA = await res.json();
    } catch (err) {
      root.innerHTML =
        '<div class="state err">Could not load votes.json (' + err.message + ").</div>";
      return;
    }
    document.getElementById("v-total").textContent = DATA.n_votes;
    document.getElementById("v-progress").textContent = DATA.n_in_progress;
    document.getElementById("v-passed").textContent = DATA.n_passed;
    document.getElementById("v-failed").textContent = DATA.n_failed;
    if (!wired) wireControls();
    render();
  }

  function wireControls() {
    wired = true;
    document.querySelectorAll("#tab-votes .vf").forEach(function (c) {
      c.onclick = function () {
        document.querySelectorAll("#tab-votes .vf").forEach(function (x) { x.classList.remove("on"); });
        c.classList.add("on");
        statusFilter = c.dataset.vf;
        render();
      };
    });
    document.getElementById("v-search").oninput = render;
    document.getElementById("v-kind").onchange = function (e) {
      kindFilter = e.target.value;
      render();
    };
  }

  function voteCard(v) {
    const st = STATUS[v.status] || { label: v.status, cls: "" };
    const c = v.counts || { favor: 0, against: 0, abstain: 0, not_voted: 0 };
    const total = c.favor + c.against + c.abstain + c.not_voted;

    let ref = "PR #" + v.pr;
    if (v.kind === "milestone" && v.milestone) {
      ref = "Milestone " + v.milestone + " \u00b7 PR #" + v.pr + " \u00b7 issue #" + v.number;
    } else if (v.kind === "proposal") {
      ref = "Proposal \u00b7 PR #" + v.pr;
    }

    const favorPct = Math.min(v.favor, 100);
    const threshPct = Math.min(v.threshold, 100);

    return (
      '<div class="vote">' +
      '<div class="vote-top">' +
      '<div class="vote-title">' +
      '<span class="kindtag">' + (KIND[v.kind] || "Vote") + "</span>" +
      escapeHtml(v.title) +
      "</div>" +
      '<span class="vstatus ' + st.cls + '">' + st.label + "</span>" +
      "</div>" +
      '<div class="vote-ref">' + ref + (v.comment_date ? " \u00b7 " + v.comment_date : "") + "</div>" +
      '<div class="tally">' +
      '<div class="tally-bar">' +
      '<div class="tally-fill" style="width:' + favorPct.toFixed(0) + '%"></div>' +
      '<div class="tally-thresh" style="left:' + threshPct.toFixed(0) + '%" title="Passing threshold: ' + v.threshold + '%"></div>' +
      "</div>" +
      '<div class="tally-legend">' +
      "<span><b>" + v.favor.toFixed(0) + "%</b> in favour</span>" +
      "<span>threshold " + v.threshold.toFixed(0) + "%</span>" +
      "</div>" +
      "</div>" +
      '<div class="vote-counts">' +
      '<span class="vc favor">' + c.favor + " for</span>" +
      '<span class="vc against">' + c.against + " against</span>" +
      '<span class="vc abstain">' + c.abstain + " abstain</span>" +
      '<span class="vc none">' + c.not_voted + " not voted</span>" +
      (total ? '<span class="vc total">' + total + " eligible</span>" : "") +
      "</div>" +
      '<a class="linkbtn" href="' + v.url + '" target="_blank" rel="noopener">View vote thread \u2197</a>' +
      "</div>"
    );
  }

  function render() {
    if (!DATA) return;
    const q = document.getElementById("v-search").value.toLowerCase();
    const rows = DATA.votes.filter(function (v) {
      return (
        (statusFilter === "all" || v.status === statusFilter) &&
        (kindFilter === "all" || v.kind === kindFilter) &&
        (!q || v.title.toLowerCase().includes(q))
      );
    });
    document.getElementById("votes").innerHTML =
      rows.length ? rows.map(voteCard).join("") : '<div class="state">No votes match.</div>';
    document.getElementById("v-count").textContent =
      rows.length + " of " + DATA.votes.length + " shown";
  }

  window.loadVotes = loadVotes;
})();
