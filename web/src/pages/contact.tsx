import Head from "next/head";

import { ContactForm } from "@/components/contact-form";
import { HeroBand } from "@/components/hero-band";
import { SectionIndex, type Section } from "@/components/section-index";
import {
  BAND_IN,
  DEEP_B,
  EYEBROW,
  FOOT_NOTE,
  H1_WIDE,
  LEDE,
  META,
  PILL,
  PLAINLIST_TLDR,
  SecHead,
  Tldr,
} from "@/components/ui";

const SECTIONS: Section[] = [{ id: "form", label: "Send a message" }];

export default function Contact() {
  return (
    <>
      <Head>
        <title>Contact Citadel</title>
        <meta
          name="description"
          content="Reach utxo AG about Citadel: a consortium work package, a pilot for your team, or a question about the system. The form relays to a human, and nothing you send is stored in the vault."
        />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
      </Head>

      <HeroBand current="/contact" wide>
        <p className={EYEBROW}>Contact</p>
        <h1 className={H1_WIDE}>
          Tell us what you are building, <span className="grad">and what is missing</span>.
        </h1>
        <div className={META}>
          <span className={PILL}>utxo AG · Zug, Switzerland</span>
          <span className={PILL}>Reply within two working days</span>
        </div>
        <Tldr label="Three reasons people write">
          <ul className={PLAINLIST_TLDR}>
            <li>
              <b className={DEEP_B}>A consortium work package.</b> Send the call identifier, the
              topic, and the piece you need covered. We reply with a work package written against
              your structure rather than ours.
            </li>
            <li>
              <b className={DEEP_B}>A pilot for your team.</b> Tell us where your knowledge currently
              lives and what you keep re-answering. We will say plainly whether Citadel helps.
            </li>
            <li>
              <b className={DEEP_B}>A question about the system.</b> Anything the{" "}
              <a href="/info">live status</a> or the{" "}
              <a href="https://github.com/masumi-network/Citadel">source</a> did not answer.
            </li>
          </ul>
        </Tldr>
      </HeroBand>

      <SectionIndex sections={SECTIONS} />

      <section className="relative bg-surface py-[74px] scroll-mt-[46px] max-[620px]:py-12" id="form">
        <div className={BAND_IN}>
          <SecHead kicker="Send a message" title="It reaches a person, not the vault" />
          <p className={LEDE}>
            What you send is relayed to our team chat and read by a human. It is never written into
            Citadel itself, so nothing here becomes memory an agent can later read back.
          </p>

          <ContactForm />

          <footer className="mt-[34px] text-[15px] text-ink-2">
            {/* The website and the city, and nothing else. A personal name, an
                email and a registered address were placeholders here for a
                while; the enquiry form above is the route in, and none of the
                three needed publishing to make it work. */}
            <p>
              <b>utxo AG</b> · Zug, Switzerland · <a href="https://utxo.ag/">utxo.ag</a>
            </p>
            <p className={FOOT_NOTE}>
              Bugs and feature requests belong in the{" "}
              <a href="https://github.com/masumi-network/Citadel/issues">public issue tracker</a>.
              Already have a seat? <a href="/login">Sign in</a>.
            </p>
          </footer>
        </div>
      </section>
    </>
  );
}
