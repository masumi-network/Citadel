/* Applies the remembered theme before the page paints.
 *
 * This is a separate file rather than an inline <script> because the site runs
 * under `script-src 'self'` and a static export cannot carry a per-response
 * nonce. It is loaded from <head> without `defer` so it runs during parse, and
 * it only ever touches one attribute, so the cost of blocking there is a
 * getItem and a setAttribute.
 *
 * `citadel-info-theme` is the same localStorage key the hand-written pages use,
 * so crossing between /next and /info keeps the visitor's choice.
 *
 * Light is the default for everyone. prefers-color-scheme is deliberately not
 * consulted: dark is an explicit, remembered choice made with the toggle.
 */
(function () {
  "use strict";
  try {
    var saved = localStorage.getItem("citadel-info-theme");
    if (saved === "light" || saved === "dark") {
      document.documentElement.setAttribute("data-theme", saved);
    }
  } catch (e) {
    /* storage blocked — light, as if nothing had ever been chosen */
  }
})();
