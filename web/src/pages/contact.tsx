import Head from "next/head";

import { ContactForm } from "@/components/contact-form";
import { HeroBand } from "@/components/hero-band";
import {
  BAND_IN,
  SECTION,
  EYEBROW,
  FOOT_NOTE,
  H1_WIDE,
  HERO_STRIP_3,
  HeroFact,
  LEDE,
  META,
  PILL,
  SecHead,
} from "@/components/ui";

export default function Contact() {
  return (
    <>
      <Head>
        <title>Citadel</title>
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
        <div className={`${META} mb-8`}>
          <span className={PILL}>utxo AG · Zug, Switzerland</span>
          <span className={PILL}>Reply within two working days</span>
        </div>
        <dl className={HERO_STRIP_3}>
          <HeroFact kicker="Consortium">
            Send the call identifier, the topic, and the piece you need covered.
          </HeroFact>
          <HeroFact kicker="Team">
            Tell us where your knowledge currently lives and what you keep re-answering.
          </HeroFact>
          <HeroFact kicker="Question">
            Ask anything the <a href="/info">live status</a> or the{" "}
            <a href="https://github.com/masumi-network/Citadel">source</a> did not answer.
          </HeroFact>
        </dl>
      </HeroBand>

      <section className={`${SECTION} bg-surface`} id="form">
        <div className={BAND_IN}>
          <div className="mx-auto max-w-[34rem]">
            <SecHead kicker="Send a message" title="It reaches a person, not the vault" />
            <p className={LEDE}>
              What you send is relayed to our team chat and read by a human. It is never written
              into Citadel itself, so nothing here becomes memory an agent can later read back.
            </p>

            <ContactForm />

            <p className="mt-8 border-t border-border pt-5 text-[13.5px] leading-[1.55] text-ink-3">
              If the form is down, or you would rather not use it, write to{" "}
              <a href="mailto:sarthi.borkar@nmkr.io">sarthi.borkar@nmkr.io</a>.
            </p>

            <footer className="mt-8 text-[15px] text-ink-2">
              {/* The website and the city. A personal name, an email and a
                  registered address were placeholders here for a while, and the
                  name and the address stayed unpublished. The email did not: it
                  now sits above this footer, by request, because the form relays
                  into a team chat and someone who will not type into a form
                  still needs a route in. Two routes, one of them a person. */}
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
        </div>
      </section>
    </>
  );
}
