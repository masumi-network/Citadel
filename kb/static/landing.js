/* Citadel — / behavior. Loads alongside info.js, which already carries the
   theme toggle and hydrates the live health pill in the section index. This
   file owns what the landing page adds on top of that: the brand mark (info.js
   paints it first; the guard keeps this file correct on its own), the sticky
   index's active entry, and fetching the React Flow diagram on demand.

   No motion lives here. The rotating headline and the drifting glow are CSS
   keyframes, because a strict CSP (script-src 'self'; style-src 'self') leaves
   a JS timer nowhere to write to. */
(function () {
  "use strict";

  // ---- Pixel Bastion mark (7x7 crenellated castle) ----
  var grid = ["1010101", "1111111", "1111111", "1111111", "1101011", "1101011", "1101011"];
  var mark = document.getElementById("mark");
  if (mark && !mark.childElementCount) {
    grid.join("").split("").forEach(function (c) {
      var cell = document.createElement("i");
      if (c === "1") cell.className = "on";
      mark.appendChild(cell);
    });
  }

  if (!("IntersectionObserver" in window)) return;

  // ---- sticky section index ----
  // The topmost band currently in view owns the underline. Tracking a set of
  // intersecting bands (rather than the last callback entry) keeps the state
  // right when a fast scroll crosses two boundaries in one frame.
  var links = Array.prototype.slice.call(document.querySelectorAll(".index a[href^='#']"));
  var bands = [];
  links.forEach(function (link) {
    var band = document.getElementById(link.getAttribute("href").slice(1));
    if (band) bands.push({ id: band.id, band: band, link: link });
  });

  if (bands.length) {
    var visible = Object.create(null);
    var indexObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        visible[entry.target.id] = entry.isIntersecting;
      });
      var active = null;
      bands.forEach(function (item) {
        if (!active && visible[item.id]) active = item.id;
      });
      bands.forEach(function (item) {
        item.link.classList.toggle("on", item.id === active);
      });
    }, { rootMargin: "-46px 0px -55% 0px" });
    bands.forEach(function (item) { indexObserver.observe(item.band); });
  }

  // ---- interactive pipeline diagram, fetched only if it is reached ----
  // React, React DOM and React Flow are ~330 KB, on a page that otherwise
  // ships almost nothing. So the .spine above is the real diagram, and this
  // upgrades it in place the first time it comes near the viewport. Someone who
  // never scrolls this far, or who has JavaScript off, keeps a correct picture
  // and never pays for the bundle.
  var staticDiagram = document.getElementById("spine-static");
  var staticRead = document.getElementById("spine-read");
  var flowRoot = document.getElementById("flow-root");
  var flowNote = document.getElementById("flow-note");
  if (!staticDiagram || !flowRoot) return;

  var requested = false;

  function swapInFlow() {
    if (!window.CitadelFlow || typeof window.CitadelFlow.mount !== "function") return;
    // Unhide before mounting: React Flow measures its container to fit the
    // graph, and a display:none container measures zero.
    flowRoot.hidden = false;
    var mounted = false;
    try {
      mounted = window.CitadelFlow.mount(flowRoot);
    } catch (e) {
      mounted = false;
    }
    if (mounted) {
      staticDiagram.hidden = true;
      // The read line is the guarantee, and the interactive diagram does not
      // restate it, so it stays on screen either way.
      if (staticRead) staticRead.hidden = false;
      if (flowNote) flowNote.hidden = false;
    } else {
      flowRoot.hidden = true;
    }
  }

  function loadFlow() {
    if (requested) return;
    requested = true;

    var styles = document.createElement("link");
    styles.rel = "stylesheet";
    styles.href = "/static/vendor/flow.css";
    document.head.appendChild(styles);

    var script = document.createElement("script");
    script.src = "/static/vendor/flow.js";
    script.defer = true;
    script.onload = swapInFlow;
    // A failed fetch is not an error state to show. The spine is already on
    // screen and already correct, so we simply keep it.
    script.onerror = function () { styles.remove(); };
    document.head.appendChild(script);
  }

  var flowObserver = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      flowObserver.disconnect();
      loadFlow();
    });
  }, { rootMargin: "300px 0px" });
  flowObserver.observe(staticDiagram);
})();
