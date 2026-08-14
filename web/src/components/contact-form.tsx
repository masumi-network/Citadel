import { useState, type FormEvent } from "react";

import { FIELD_HINT, FIELD_INPUT, FIELD_LABEL, SUBMIT } from "@/components/ui";

const OK = "Thanks. It reached us, and you will hear back within two working days.";
const GENERIC_FAILURE = "That did not go through. Please try again.";

type Note = { text: string; ok: boolean } | null;

/* The partnering enquiry form.
 *
 * POSTs to /contact, which relays to the org's team chat and never writes to
 * the vault (ADR-0013). Every behaviour of that endpoint is preserved here,
 * because they are the ones a visitor actually experiences:
 *
 * - The `website` field is a honeypot. A human never sees it, so anything in it
 *   marks a bot; the server answers 200 either way so a bot cannot learn it was
 *   filtered. It is positioned off-screen rather than display:none, which some
 *   bots skip.
 * - Rate limiting answers 429, a missing chat gateway answers 503, and a failed
 *   delivery answers 502. All three carry a `detail` the visitor should read,
 *   so the message from the server is shown as-is rather than replaced with a
 *   generic apology. A 503 on a node with no gateway configured is correct
 *   behaviour, not a bug: an enquiry is never accepted into a void.
 */
export function ContactForm() {
  const [note, setNote] = useState<Note>(null);
  const [sending, setSending] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    setNote(null);
    setSending(true);

    const data = new FormData(form);
    try {
      const response = await fetch("/contact", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: data.get("name") || "",
          email: data.get("email") || "",
          organization: data.get("organization") || "",
          message: data.get("message") || "",
          website: data.get("website") || "",
        }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || GENERIC_FAILURE);
      }
      form.reset();
      setNote({ text: OK, ok: true });
    } catch (error) {
      setNote({ text: error instanceof Error ? error.message : GENERIC_FAILURE, ok: false });
    } finally {
      setSending(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="flex max-w-[640px] flex-col gap-3.5">
      <div className="grid grid-cols-2 gap-3.5 max-[620px]:grid-cols-1">
        <div className="flex flex-col gap-[7px]">
          <label className={FIELD_LABEL} htmlFor="cf-name">
            Your name
          </label>
          <input
            className={FIELD_INPUT}
            id="cf-name"
            name="name"
            type="text"
            maxLength={120}
            required
            autoComplete="name"
          />
        </div>
        <div className="flex flex-col gap-[7px]">
          <label className={FIELD_LABEL} htmlFor="cf-email">
            Email
          </label>
          <input
            className={FIELD_INPUT}
            id="cf-email"
            name="email"
            type="email"
            maxLength={200}
            required
            autoComplete="email"
          />
        </div>
      </div>
      <div className="flex flex-col gap-[7px]">
        <label className={FIELD_LABEL} htmlFor="cf-org">
          Organisation
        </label>
        <input
          className={FIELD_INPUT}
          id="cf-org"
          name="organization"
          type="text"
          maxLength={160}
          autoComplete="organization"
        />
      </div>
      <div className="flex flex-col gap-[7px]">
        <label className={FIELD_LABEL} htmlFor="cf-msg">
          What you need
        </label>
        <textarea
          className={`${FIELD_INPUT} min-h-[130px] resize-y leading-[1.6]`}
          id="cf-msg"
          name="message"
          rows={6}
          maxLength={4000}
          required
        />
        <p className={FIELD_HINT}>
          Writing about a call? Include the call identifier, the topic, and the gap you need covered.
        </p>
      </div>

      {/* Honeypot. Off-screen rather than display:none, which some bots skip. */}
      <div className="absolute -left-[9999px] h-px w-px overflow-hidden" aria-hidden="true">
        <label htmlFor="cf-web">Leave this field empty</label>
        <input id="cf-web" name="website" type="text" tabIndex={-1} autoComplete="off" />
      </div>

      <p
        className={`text-[13px] leading-[1.6] ${note?.ok ? "text-good" : "text-warn"}`}
        role="status"
      >
        {note?.text ?? ""}
      </p>
      <button type="submit" disabled={sending} aria-busy={sending} className={`${SUBMIT} self-start mt-1`}>
        {sending ? "Sending" : "Send enquiry"}
      </button>
    </form>
  );
}
