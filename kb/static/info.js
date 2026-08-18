/* Citadel — /info page behavior. Loaded as an external script (CSP script-src
   'self'). No inline handlers. Chart bar heights use CSSOM (allowed under a
   strict style-src). */
(function () {
  "use strict";

  // ---- theme (prefers-color-scheme by default; toggle persists an override) ----
  var root = document.documentElement;
  try {
    var saved = localStorage.getItem("citadel-info-theme");
    if (saved === "light" || saved === "dark") root.setAttribute("data-theme", saved);
  } catch (e) { /* storage blocked — fall back to prefers-color-scheme */ }

  // Light is the default. Dark is only ever an explicit, remembered choice, so
  // the OS preference is deliberately not consulted.
  function currentTheme() {
    return root.getAttribute("data-theme") || "light";
  }
  var btn = document.getElementById("themebtn");
  function updateBtn() {
    if (btn) btn.textContent = currentTheme() === "dark" ? "☾ Dark" : "☀ Light";
  }
  if (btn) {
    updateBtn();
    btn.addEventListener("click", function () {
      var next = currentTheme() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("citadel-info-theme", next); } catch (e) { /* ignore */ }
      updateBtn();
    });
  }

  // ---- Pixel Bastion mark (7x7 crenellated castle) ----
  var grid = ["1010101", "1111111", "1111111", "1111111", "1101011", "1101011", "1101011"];
  var mark = document.getElementById("mark");
  if (mark) {
    grid.join("").split("").forEach(function (c) {
      var cell = document.createElement("i");
      if (c === "1") cell.className = "on";
      mark.appendChild(cell);
    });
  }

  // ---- use-cases audience (Team / Partner) ----
  // Partner copy stays in the document. Default is Team: those blocks carry
  // hidden until the visitor asks. A #ask (or other partner) hash opens Partner
  // so a coordinator link still lands on the work-package text.
  var audience = document.getElementById("audience");
  if (audience) {
    var teamBtn = document.getElementById("audience-team");
    var partnerBtn = document.getElementById("audience-partner");
    var partnerBlocks = document.querySelectorAll("[data-audience='partner']");
    var partnerHashes = { fit: 1, can: 1, wp: 1, ask: 1, talk: 1 };
    function setPartner(on) {
      for (var i = 0; i < partnerBlocks.length; i++) partnerBlocks[i].hidden = !on;
      if (teamBtn) teamBtn.setAttribute("aria-pressed", on ? "false" : "true");
      if (partnerBtn) partnerBtn.setAttribute("aria-pressed", on ? "true" : "false");
      var hash = location.hash.replace(/^#/, "");
      if (!on && partnerHashes[hash]) {
        history.replaceState(null, "", location.pathname + location.search);
      }
    }
    if (teamBtn) teamBtn.addEventListener("click", function () { setPartner(false); });
    if (partnerBtn) partnerBtn.addEventListener("click", function () { setPartner(true); });
    var landing = location.hash.replace(/^#/, "");
    if (partnerHashes[landing]) {
      setPartner(true);
      var target = document.getElementById(landing);
      if (target) target.scrollIntoView();
    }
  }

  // ---- commits per week ----
  // The baked series is the fallback, drawn immediately so the chart is never
  // an empty box. It came from git log at report time, which is also why it
  // cannot refresh itself: the deployed node has no git and no repository, only
  // the built image. /api/state carries the live weekly counts from GitHub, and
  // drawChart re-runs with those once the fetch lands.
  var weeks = [
    { l: "May 18", v: 9 },
    { l: "May 25", v: 24 },
    { l: "Jun 1", v: 30 },
    { l: "Jun 8", v: 26 },
    { l: "Jun 15", v: 20 },
    { l: "Jun 22", v: 91, tag: "v0.1.x" },
    { l: "Jun 29", v: 78, tag: "v0.2.0–2.2" },
    { l: "Jul 6", v: 5, tag: "v0.2.3" },
    { l: "Jul 13", v: 41, tag: "v0.3.0" },
    { l: "Jul 20", v: 53, tag: "v0.4.0" }
  ];
  var chart = document.getElementById("chart");

  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function weekLabel(iso) {
    var parts = String(iso).split("-");
    if (parts.length !== 3) return String(iso);
    var month = MONTHS[parseInt(parts[1], 10) - 1];
    if (!month) return String(iso);
    return month + " " + parseInt(parts[2], 10);
  }

  // Release markers are a local fact, not something GitHub returns, so carry
  // them across onto whichever live week they contain.
  //
  // Matching by label string does not work and silently loses every marker:
  // the baked series is keyed to Mondays ("Jun 22") while GitHub's
  // commit_activity weeks start on Sunday ("Jun 21"), so the lookup misses by
  // one day on every row, no bar ever gets the `ship` class, and the chart
  // renders entirely grey. Match by date range instead, which is immune to
  // whichever weekday either side considers the start of a week.
  var RELEASES = [
    { on: "2026-06-22", tag: "v0.1.x" },
    { on: "2026-06-29", tag: "v0.2.0–2.2" },
    { on: "2026-07-06", tag: "v0.2.3" },
    { on: "2026-07-13", tag: "v0.3.0" },
    { on: "2026-07-20", tag: "v0.4.0" }
  ];
  var WEEK_MS = 7 * 24 * 60 * 60 * 1000;
  function tagForWeek(startIso) {
    var start = Date.parse(startIso);
    if (isNaN(start)) return "";
    for (var i = 0; i < RELEASES.length; i++) {
      var on = Date.parse(RELEASES[i].on);
      if (!isNaN(on) && on >= start && on < start + WEEK_MS) return RELEASES[i].tag;
    }
    return "";
  }

  function drawChart(series) {
    if (!chart || !series.length) return;
    chart.textContent = "";
    var max = series.reduce(function (m, w) { return Math.max(m, w.v); }, 1);
    series.forEach(function (w) {
      var col = document.createElement("div");
      col.className = "bar-col" + (w.tag ? " ship" : "");
      col.title = w.v + " commits · week of " + w.l + (w.tag ? " · " + w.tag : "");
      var val = document.createElement("div"); val.className = "bar-val"; val.textContent = w.v;
      var bar = document.createElement("div"); bar.className = "bar";
      bar.style.height = Math.max(3, Math.round(w.v / max * 104)) + "px";
      var tag = document.createElement("div"); tag.className = "bar-tag"; tag.textContent = w.tag || "";
      var lbl = document.createElement("div"); lbl.className = "bar-lbl"; lbl.textContent = w.l;
      col.appendChild(val); col.appendChild(bar); col.appendChild(tag); col.appendChild(lbl);
      chart.appendChild(col);
    });
    var peak = series.reduce(function (m, w) { return w.v > m.v ? w : m; }, series[0]);
    chart.setAttribute(
      "aria-label",
      "Commits per week on the main branch, " + series[0].l + " to " +
        series[series.length - 1].l + ", peaking at " + peak.v +
        " in the week of " + peak.l + "."
    );
  }

  drawChart(weeks);

  // ---- live tiles from /api/state ----
  function rel(iso) {
    if (!iso) return "";
    var t = Date.parse(iso);
    if (isNaN(t)) return "";
    var mins = Math.round((Date.now() - t) / 60000);
    if (mins < 2) return "just now";
    if (mins < 60) return mins + " min ago";
    var hrs = Math.round(mins / 60);
    if (hrs < 24) return hrs + (hrs === 1 ? " hour ago" : " hours ago");
    var days = Math.round(hrs / 24);
    if (days < 8) return days + (days === 1 ? " day ago" : " days ago");
    return "on " + new Date(t).toISOString().slice(0, 10);
  }
  function vlabel(v) {
    if (!v) return "";
    return /^[0-9]/.test(v) ? "v" + v : v;
  }
  function set(id, text) { var el = document.getElementById(id); if (el) el.textContent = text; }

  fetch("/api/state", { headers: { "Accept": "application/json" } })
    .then(function (r) { if (!r.ok) throw new Error("state " + r.status); return r.json(); })
    .then(function (d) {
      var ver = vlabel(d.version) || "v0.5.0";
      set("m-version", ver);
      var healthEl = document.getElementById("pill-health");
      var healthText = document.getElementById("pill-health-text");
      if (d.healthy === false) {
        if (healthEl) healthEl.classList.add("down");
        if (healthText) healthText.textContent = "Degraded · " + ver;
      } else if (healthText) {
        healthText.textContent = "Live · " + ver;
      }

      var gh = (d.sources || []).filter(function (s) { return s.type === "github"; })[0];
      var repos = (d.totals && d.totals.github_repositories) || (gh && gh.documents) || 0;
      var docsEl = document.getElementById("m-docs");
      if (docsEl) docsEl.innerHTML = repos + " <small>repos</small>";
      var when = gh && gh.last_synced_at ? rel(gh.last_synced_at) : "";
      set("m-docs-sub", when ? "GitHub org synced · " + when : "GitHub org not synced on this node");

      // ---- repo figures ----
      // mcp_tools is computed fresh on every /api/state call (a policy-table
      // length, not a cache), so it carries no "refreshed X ago" note below.
      // Commit and ADR counts used to live here too; they are gone from the
      // page by design (repo trivia, not evidence the system works), but the
      // weekly commit chart stays as recent git activity.
      var repo = d.repo || {};
      if (typeof repo.mcp_tools === "number") set("m-tools", repo.mcp_tools);
      if (repo.weeks && repo.weeks.length) {
        // The layout fits ~12 columns; GitHub can report up to 52 weeks.
        // Newest win, so the chart stays readable whatever the API returns.
        drawChart(repo.weeks.slice(-12).map(function (w) {
          return { l: weekLabel(w.start), v: w.commits, tag: tagForWeek(w.start) };
        }));
      }

      var repoAge = repo.refreshed_at ? rel(repo.refreshed_at) : "";
      var repoNote;
      if (repo.source !== "github") {
        // No successful fetch yet: the chart is showing the baked series.
        repoNote = " The commit-activity chart has not refreshed yet.";
      } else if (repo.stale) {
        repoNote = " The commit-activity chart last refreshed " + repoAge + ".";
      } else {
        repoNote = " The commit-activity chart refreshed " + repoAge + ".";
      }

      var upd = rel(d.updated_at);
      set("state-updated", "Live tiles updated" + (upd ? " " + upd : "") + "." +
        repoNote + " Releases are as of v0.5.0, 2026-08-14.");
      set("foot-note", "State-of-the-vault report · live tiles from /api/state" +
        (upd ? " (updated " + upd + ")" : "") + " · window v0.2.0 → v0.5.1.");
    })
    .catch(function () {
      set("m-docs", "—");
      set("m-docs-sub", "GitHub org sync (live data unavailable)");
      set("state-updated", "Live data unavailable right now. Showing the last published repo figures, as of v0.5.0, 2026-08-14.");
    });
})();
