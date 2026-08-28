import Link from "next/link";

export default function Home() {
  return (
    <main className="wrap">
      <section className="hero">
        {/* Inherited from the Halcyon baseline (frontend/app/page.tsx, 2023-11).
            These three strings are the platform over-claiming its compliance
            posture -- kept because that over-claim is the artifact, and labelled
            because an unlabelled one reads as Meridian's own current assertion.

            README.md: "Treat any prior claim of PCI-DSS compliance for this
            codebase as false", and SOX/ECOA process claims beyond the decision
            audit trail are unverified. ARCHITECTURE.md: nothing here asserts
            regulatory compliance. docs/presentations/2026-08-25-agentic-client-
            handoff.md lists 'PCI compliant' under "Claims we must NOT make".

            The invariant, pinned by frontend/e2e/inherited-compliance-claims.spec.ts:
            if the claims are shown, the qualifier is shown with them. Removing
            the claims entirely is a valid future remediation and the test allows
            it -- what it forbids is showing them bare. See docs/DEBT.md D25. */}
        <div className="badge-row" style={{ marginBottom: 16 }}>
          <span className="badge badge-inherited" data-testid="inherited-claims-qualifier">
            Inherited vendor claims &mdash; not verified by Meridian
          </span>
          <span className="badge">SOX-controlled</span>
          <span className="badge">PCI compliant</span>
          <span className="badge">ECOA / Reg B</span>
        </div>

        <h1>Personal loans, decided fast and disclosed honestly.</h1>
        <p className="hero-lede">
          Meridian Lending offers fixed-rate personal installment loans from{" "}
          <strong>$1,000 to $50,000</strong>, terms of 12 to 60 months. Check
          your offer, review your Truth-in-Lending disclosure up front, and
          manage your loan online.
        </p>

        <div className="btn-row">
          <Link href="/apply" className="btn">
            Apply for a loan
          </Link>
          <Link href="/servicing" className="btn btn-ghost">
            Servicing dashboard
          </Link>
        </div>
      </section>

      <h2>Why Meridian</h2>
      <div className="grid grid-3">
        <div className="feature">
          <div className="feature-icon" aria-hidden>
            ⚡
          </div>
          <h3>Fast decisions</h3>
          <p>
            {/* "Soft-pull pre-qualification" was removed rather than reworded
                around. Nothing in specs/, adr/, docs/, policies/ or the client
                package approves that claim, and the code contradicts it:
                `decision-service/app/bureau.py` is a stub, and its own comment
                describes the real thing as "a second, independently-billed HARD
                credit pull". A soft pull is a specific, consumer-visible
                promise about what reaches a credit file -- not a synonym for
                "quick". What is left is what the system actually does and what
                `decision.py` is tested on. */}
            Automated underwriting with clear approve, refer, or decline
            outcomes — each with a specific reason on record.
          </p>
        </div>
        <div className="feature">
          <div className="feature-icon" aria-hidden>
            📄
          </div>
          <h3>Transparent disclosures</h3>
          <p>
            See your APR, finance charge, amount financed, and total of payments
            in a standard Truth-in-Lending box before you accept anything.
          </p>
        </div>
        <div className="feature">
          <div className="feature-icon" aria-hidden>
            💳
          </div>
          <h3>Manage your loan online</h3>
          <p>
            Track your balance, view your amortization schedule and payment
            history, and make a payment from one servicing dashboard.
          </p>
        </div>
      </div>

      <div className="card" style={{ marginTop: 28 }}>
        <div className="spread">
          <div>
            <h3 style={{ marginBottom: 4 }}>Ready to see your rate?</h3>
            <p className="muted" style={{ margin: 0 }}>
              Checking your offer takes a few minutes and won&apos;t affect your
              ability to apply.
            </p>
          </div>
          <Link href="/apply" className="btn">
            Start application
          </Link>
        </div>
      </div>
    </main>
  );
}
