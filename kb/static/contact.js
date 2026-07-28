/* Citadel — /partners contact form. Posts to /contact, which relays to the
   org's Google Chat space. Loaded as an external script under a strict CSP:
   no inline handlers, no inline styles. */
(function () {
  "use strict";

  var form = document.getElementById("contactForm");
  if (!form) return;
  var note = document.getElementById("contactNote");
  var button = document.getElementById("contactSubmit");

  function say(text, ok) {
    note.textContent = text;
    note.classList.toggle("contact-ok", Boolean(ok));
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    say("", false);
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "Sending";

    var data = new FormData(form);
    fetch("/contact", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: data.get("name") || "",
        email: data.get("email") || "",
        organization: data.get("organization") || "",
        message: data.get("message") || "",
        website: data.get("website") || ""
      })
    })
      .then(function (response) {
        if (response.ok) return null;
        return response.json()
          .catch(function () { return {}; })
          .then(function (body) {
            throw new Error(body.detail || "That did not go through. Please try again.");
          });
      })
      .then(function () {
        form.reset();
        say("Thanks. It reached us, and you will hear back within two working days.", true);
      })
      .catch(function (err) {
        say(err.message, false);
      })
      .finally(function () {
        button.disabled = false;
        button.setAttribute("aria-busy", "false");
        button.textContent = "Send enquiry";
      });
  });
})();
