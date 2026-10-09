"""Generate the Meridian Lending logical architecture diagram (light + dark SVG).

Standard library only, so the diagram can be regenerated anywhere:

    python docs/diagrams/generate_diagrams.py            # writes into docs/

Every box, arrow and label is a claim about the repository, and
`db/tests/test_public_docs_match_shipped_behaviour.py` checks the rendered SVGs:
each backend service under `services/` and each gateway route prefix must appear
in the text labels. Edit this file, regenerate, and commit both SVGs together.

Visual language: rounded boxes with a bold title and muted detail lines, dashed
trust-boundary groups, and explicit triangle arrowheads (SVG <marker> is not
rendered by every viewer).
"""
import math
import pathlib
import sys
from html import escape

THEMES = {
    "light": dict(bg="#ffffff", box="#f3f5fb", box2="#fafbff", group="#fafbff", group_stroke="#9aa3d9",
                  zone="#fcfcfe", zone_stroke="#b8bdd6",
                  stroke="#3f4ab8", title="#14213d", text="#4b587c", accent="#3f4ab8",
                  green="#2e7d4f", green_fill="#eef7f1", amber="#9a6700", amber_fill="#fff8e6",
                  data_fill="#eef6fb", data="#0b6f8a", ops_fill="#f6f6f8", ops="#5b6275"),
    "dark": dict(bg="#0d1117", box="#161b22", box2="#11161d", group="#11161d", group_stroke="#4a5280",
                 zone="#0f141b", zone_stroke="#3a4050",
                 stroke="#8b93e8", title="#e6edf3", text="#9aa4b2", accent="#8b93e8",
                 green="#56c88a", green_fill="#132a1d", amber="#e3b341", amber_fill="#2b2410",
                 data_fill="#0f2430", data="#4fb3cf", ops_fill="#161a21", ops="#9aa4b2"),
}

ALT = ("Meridian Lending logical architecture. Borrowers and staff use the Next.js portal, which sends "
       "web/API requests to the gateway BFF (session auth, RBAC, rate limits; routes /auth, /los, /lss, /kyc, "
       "/decision, /disclosure, /payments, /assistant). Inside the private Compose network, origination-service "
       "is the system of record and makes internal calls to kyc-service, decision-service (a LangGraph state "
       "graph) and disclosure-service (driven by a two-node LangGraph disclosure orchestration); servicing-service holds the "
       "append-only ledger, maker-checker and reconciliation, and payment-service applies captured payments to "
       "it. Seven services share one PostgreSQL schema; Redis holds gateway sessions and rate limits. A "
       "staff-only advisory AI lane holds loan-assistant (RAG policy chat and a LangChain agent on AWS Bedrock "
       "with one bounded read-only policy tool), which reads origination read-only and has no database "
       "connection. Operations: Prometheus, Grafana, LangSmith tracing and CI.")


class Svg:
    def __init__(self, w, h, t, label):
        self.w, self.h, self.t, self.parts, self.label = w, h, t, [], label

    def rect(self, x, y, w, h, fill, stroke, dash=False, rx=12, sw=2):
        d = ' stroke-dasharray="7 6"' if dash else ""
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
                          f'stroke="{stroke}" stroke-width="{sw}"{d}/>')

    def text(self, x, y, s, size=15, weight=400, color=None, anchor="middle", italic=False):
        st = ' font-style="italic"' if italic else ""
        self.parts.append(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" '
                          f'fill="{color or self.t["text"]}" text-anchor="{anchor}"{st}>{escape(s)}</text>')

    def zone(self, x, y, w, h, label, stroke=None, fill=None, anchor="end"):
        self.rect(x, y, w, h, fill or self.t["zone"], stroke or self.t["zone_stroke"], dash=True, rx=18, sw=1.5)
        lx = x + w - 18 if anchor == "end" else x + 18
        self.text(lx, y + 24, label.upper(), 12.5, 700, stroke or self.t["ops"], anchor)

    def box(self, x, y, w, h, title, lines=(), title_color=None, fill=None, stroke=None, dash=False, size=18):
        self.rect(x, y, w, h, fill or self.t["box"], stroke or self.t["stroke"], dash)
        n = 1 + len(lines)
        top = y + h / 2 - (n - 1) * 10.5 + 6
        self.text(x + w / 2, top, title, size, 700, title_color or self.t["title"])
        for i, ln in enumerate(lines):
            self.text(x + w / 2, top + 23 + i * 20, ln, 14)

    def arrow(self, pts, color=None, label=None, lx=None, ly=None, dash=False, anchor="middle"):
        c = color or self.t["accent"]
        d = ' stroke-dasharray="7 6"' if dash else ""
        (x1, y1), (x2, y2) = pts[-2], pts[-1]
        a = math.atan2(y2 - y1, x2 - x1)
        hl, hw = 13, 7
        bx, by = x2 - hl * math.cos(a), y2 - hl * math.sin(a)
        line = list(pts[:-1]) + [(bx, by)]
        path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(line))
        self.parts.append(f'<path d="{path}" fill="none" stroke="{c}" stroke-width="2.5"{d}/>')
        p1 = (bx + hw * math.sin(a), by - hw * math.cos(a))
        p2 = (bx - hw * math.sin(a), by + hw * math.cos(a))
        self.parts.append(f'<path d="M{x2:.1f},{y2:.1f} L{p1[0]:.1f},{p1[1]:.1f} '
                          f'L{p2[0]:.1f},{p2[1]:.1f} z" fill="{c}"/>')
        if label:
            self.text(lx, ly, label, 13.5, 600, c, anchor)

    def line(self, pts, color):
        path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(pts))
        self.parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.5"/>')

    def render(self):
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" width="{self.w}" '
                f'height="{self.h}" role="img" aria-label="{escape(self.label)}">\n'
                '<style>text{font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}</style>\n'
                f'<rect width="{self.w}" height="{self.h}" fill="{self.t["bg"]}"/>\n'
                + "\n".join(self.parts) + "\n</svg>\n")


def meridian(t):
    W, H = 1660, 1110
    s = Svg(W, H, t, ALT)

    # ---- trust boundaries (drawn first, behind everything) ----------------------------
    s.zone(30, 20, 1160, 132, "Public · client")
    s.zone(30, 168, 1160, 168, "Application edge")
    s.zone(30, 352, 1160, 470, "Private Compose network · lending services")
    s.zone(30, 838, 1160, 152, "Data", stroke=t["data"], fill=t["zone"])
    s.zone(1275, 168, 355, 822, "AI advisory boundary · staff-only", stroke=t["green"], fill=t["zone"],
           anchor="start")

    # ---- client -------------------------------------------------------------------------
    s.box(80, 52, 330, 84, "Borrower · Staff", ["csr · underwriter · admin · borrower"], fill=t["box2"])
    s.box(560, 52, 580, 84, "Next.js 15 portal", ["application wizard · servicing dashboard · staff views"],
          fill=t["box2"])
    s.arrow([(410, 94), (560, 94)])

    # ---- edge ---------------------------------------------------------------------------
    s.box(80, 200, 1080, 118, "gateway · BFF",
          ["session auth · RBAC · rate limits · strips inbound identity headers · signs principal",
           "/auth · /los · /lss · /kyc · /decision · /disclosure · /payments · /assistant"],
          title_color=t["accent"], fill=t["box2"])
    s.arrow([(850, 136), (850, 200)], label="Web/API request", lx=862, ly=176, anchor="start")

    # ---- servicing group ----------------------------------------------------------------
    s.rect(60, 392, 510, 410, t["group"], t["group_stroke"], dash=True, rx=16)
    s.text(118, 418, "Servicing", 15, 700, t["accent"], "start")
    s.box(130, 440, 420, 120, "servicing-service",
          ["append-only ledger · maker-checker", "reconciliation job + human review queue",
           "verifies the signed staff principal"])
    s.box(130, 650, 420, 104, "payment-service",
          ["tokenized capture · no PAN/CVV stored", "idempotency key on every charge"])
    s.arrow([(340, 650), (340, 560)], label="apply-payment", lx=352, ly=610, anchor="start")
    s.arrow([(340, 318), (340, 440)], label="/lss", lx=352, ly=420, anchor="start")
    s.arrow([(100, 318), (100, 702), (130, 702)], label="/payments", lx=110, ly=632, anchor="start")

    # ---- origination group --------------------------------------------------------------
    s.rect(600, 392, 570, 410, t["group"], t["group_stroke"], dash=True, rx=16)
    s.text(620, 418, "Origination", 15, 700, t["accent"], "start")
    s.box(640, 440, 510, 120, "origination-service",
          ["system of record · intake · loan boarding", "decision finality · append-only decision_events",
           "two-node LangGraph disclosure orchestration"])
    s.arrow([(780, 318), (780, 440)], label="/los", lx=792, ly=420, anchor="start")
    trunk = 662
    s.line([(trunk, 560), (trunk, 763)], t["accent"])
    s.text(trunk + 10, 579, "internal call", 13.5, 600, t["accent"], "start")
    for i, (title, line) in enumerate([("kyc-service", "identity check (CIP)"),
                                       ("decision-service", "credit scoring · LangGraph state graph"),
                                       ("disclosure-service", "TILA offer · APR · amortization")]):
        by = 588 + i * 70
        s.arrow([(trunk, by + 30), (700, by + 30)])
        s.box(700, by, 450, 60, title, [line], size=16)

    # ---- data ---------------------------------------------------------------------------
    s.box(80, 876, 700, 94, "PostgreSQL 16", ["one shared schema used by seven services (ADR 0002)"],
          title_color=t["data"], fill=t["data_fill"], stroke=t["data"])
    s.box(830, 876, 330, 94, "Redis 7", ["gateway sessions · rate limits"],
          title_color=t["data"], fill=t["data_fill"], stroke=t["data"])
    s.arrow([(340, 802), (340, 876)], color=t["data"], label="SQL", lx=352, ly=867, anchor="start")
    s.arrow([(700, 802), (700, 876)], color=t["data"], label="SQL", lx=712, ly=867, anchor="start")

    # ---- AI advisory lane ---------------------------------------------------------------
    s.box(1300, 400, 310, 170, "loan-assistant",
          ["RAG policy chat", "LangChain agent on AWS Bedrock", "advisory only · not a decision",
           "no database connection"],
          title_color=t["green"], fill=t["green_fill"], stroke=t["green"])
    s.arrow([(1160, 259), (1455, 259), (1455, 400)], color=t["green"],
            label="/assistant", lx=1467, ly=330, anchor="start")
    s.text(1467, 350, "staff-only", 13.5, 600, t["green"], "start")
    # read-only HTTP read of application data from origination
    s.arrow([(1300, 500), (1150, 500)], color=t["green"], dash=True, label="read-only", lx=1232, ly=490)
    s.box(1300, 640, 310, 100, "bounded policy tool",
          ["read-only · approved policy corpus", "retrieval eval: rag_eval.py"],
          title_color=t["green"], fill=t["bg"], stroke=t["green"], size=16)
    s.arrow([(1455, 570), (1455, 640)], color=t["green"], label="tool call", lx=1467, ly=610, anchor="start")
    s.box(1300, 800, 310, 120, "LangSmith",
          ["AI observability · tracing", "metadata-only · optional", "not in the data path"],
          title_color=t["green"], fill=t["bg"], stroke=t["green"], dash=True, size=16)

    # ---- operations strip ---------------------------------------------------------------
    s.rect(30, 1010, 1600, 80, t["ops_fill"], t["ops"], rx=14, sw=1.5)
    s.text(54, 1036, "OPERATIONS · CROSS-CUTTING", 12.5, 700, t["ops"], "start")
    s.text(830, 1068, "Prometheus metrics  ·  Grafana dashboards  ·  LangSmith tracing  ·  "
                      "structured logs + correlation IDs  ·  CI (GitHub Actions)", 15, 600, t["title"])
    return s.render()


def main(out_dir):
    out = pathlib.Path(out_dir)
    for theme, suffix in (("light", ""), ("dark", "-dark")):
        (out / f"meridian-architecture{suffix}.svg").write_text(meridian(THEMES[theme]), encoding="utf-8",
                                                                  newline="\n")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
