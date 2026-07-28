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

  // ---- commits per week (baked from git log at report time) ----
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
  if (chart) {
    var max = weeks.reduce(function (m, w) { return Math.max(m, w.v); }, 1);
    weeks.forEach(function (w) {
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
  }

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
      var ver = vlabel(d.version) || "v0.4.0";
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
      set("m-docs-sub", "GitHub org synced" + (when ? " · " + when : ""));

      var upd = rel(d.updated_at);
      set("state-updated", "Live tiles updated" + (upd ? " " + upd : "") +
        " · repo facts (commits, tests, LOC) as of v0.4.0, 2026-07-22.");
      set("foot-note", "State-of-the-vault report · live tiles from /api/state" +
        (upd ? " (updated " + upd + ")" : "") + " · window v0.2.0 → v0.4.0.");
    })
    .catch(function () {
      set("m-docs", "—");
      set("m-docs-sub", "GitHub org sync (live data unavailable)");
      set("state-updated", "Live data unavailable right now — showing repo facts as of v0.4.0, 2026-07-22.");
    });
})();
