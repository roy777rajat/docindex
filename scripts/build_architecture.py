"""Generate the animated architecture diagram (pure SVG).
   python scripts/build_architecture.py  -> docs/assets/architecture.svg  (+ prints the step list as Markdown)

Solid outline = built and deployed. Dashed outline + PLANNED = not built yet (sources, cron/event triggers, human review, query-api)."""
from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docs" / "assets" / "architecture.svg"
FONT = "font-family=\"'Segoe UI',Roboto,Helvetica,Arial,sans-serif\""
W, H = 1400, 880
STEP_S = 6.5

SKY, TEAL, PURPLE, ORANGE, GREEN, PINK, YELLOW, RED, INDIGO = (
    "#38bdf8", "#2dd4bf", "#a78bfa", "#fb923c", "#22c55e", "#ec4899", "#facc15", "#f87171", "#818cf8")

R: dict[str, tuple[float, float, float, float]] = {}   # id -> x, y, w, h (for highlights)
body: list[str] = []


def esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def card(cid, x, y, w, h, color, title, sub="", planned=False, size=13, top=False):
    R[cid] = (x, y, w, h)
    dash = ' stroke-dasharray="6 4"' if planned else ""
    body.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="11" fill="{color}" fill-opacity="0.15" stroke="{color}" stroke-width="2"{dash}/>')
    ty = y + (h / 2 + 1 if not (sub or top) else 19 if sub else 24)
    body.append(f'<text x="{x + 12}" y="{ty}" font-size="{size}" font-weight="700" fill="#ffffff" {FONT}>{esc(title)}</text>')
    for k, ln in enumerate([sub] if isinstance(sub, str) and sub else (sub or [])):
        body.append(f'<text x="{x + 12}" y="{y + 35 + k * 15}" font-size="10.5" fill="#cbd5ff" {FONT}>{esc(ln)}</text>')
    if planned:
        body.append(f'<text x="{x + w - 8}" y="{y + 13}" text-anchor="end" font-size="8.5" font-weight="800" letter-spacing="1" fill="{color}" {FONT}>PLANNED</text>')


def heading(x, y, text, color):
    body.append(f'<text x="{x}" y="{y}" font-size="11.5" font-weight="800" letter-spacing="1.6" fill="{color}" {FONT}>{esc(text)}</text>')


def arrow(path, color="#a5b4fc", dashed=False, label="", lx=0, ly=0, both=False):
    d = ' stroke-dasharray="5 4"' if dashed else ""
    ms = ' marker-start="url(#ahs)"' if both else ""
    body.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2"{d} marker-end="url(#ah)"{ms} opacity="0.9"/>')
    if label:
        body.append(f'<text x="{lx}" y="{ly}" text-anchor="middle" font-size="9" fill="{color}" {FONT}>{esc(label)}</text>')


T = 0.0
STEPS: list[tuple[str, list[str], str]] = [
    ("Files arrive from an application, a manual upload, an email inbox or SharePoint.",
     ["src_app", "src_man", "src_mail", "src_sp"], "M210,207 H250"),
    ("Today they come in through the upload API. A cron pull or an event (planned) feeds the same entry point.",
     ["trig_api", "trig_evt", "trig_cron"], "M440,367 H455 V242 H485"),
    ("Every call is authenticated: Bearer secret, constant-time compare, closed if missing. Each service holds one IAM tier's key only.",
     ["auth_bearer", "auth_iam", "ch_upload"], ""),
    ("upload-service hashes the file (SHA-256), stores the untouched original in S3 and registers it in DynamoDB, then starts the workflow.",
     ["ch_upload", "s3", "ddb"], "M870,396 H945"),
    ("Step Functions (cleandocs-ingestion) calls Render: validate, unzip ZIPs safely, and list every file in a manifest.",
     ["sfn", "ch_validation", "ch_archive", "ch_manifest"], "M945,304 H870"),
    ("EventBridge wakes the dispatcher every 5 minutes. SQS feeds one file at a time through the DynamoDB gate. Failures retry, then go to the DLQ.",
     ["eb", "sqs", "ddb"], ""),
    ("Workflow cleandocs-file calls the Render services in order: normalize, clean, duplicate check, extract.",
     ["sfn", "ch_normalization", "ch_cleaning", "ch_duplicate", "ch_extraction"], ""),
    ("Scanned pages go to Textract for OCR (monthly page cap). The confidence score is stored with the text.",
     ["ch_extraction", "textract"], "M870,534 H945"),
    ("Confidence below 75? The file is parked and a person is notified (Slack, Teams or email). Approved or corrected, it resumes. (planned)",
     ["ch_extraction", "human"], "M485,530 H440 V546 H485"),
    ("Metadata, enrichment (Nova Micro labels), layout-aware chunking, then embedding writes chunk + metadata files to S3 (kb-source).",
     ["ch_metadata", "ch_enrichment", "ch_chunking", "ch_embedding", "nova", "s3"], "M870,580 H945"),
    ("Bedrock Knowledge Base embeds the chunks (Titan V2) into S3 Vectors. Reconciliation re-checks S3 and the index before COMPLETED.",
     ["ch_vector", "ch_recon", "kb", "s3v"], "M870,626 H945"),
    ("Next phase: query-api retrieves from the Knowledge Base with an owner filter and returns answers with citations. (planned)",
     ["query"], ""),
]
T = STEP_S * len(STEPS)


def nk(t: float) -> str:
    return f"{max(0.0, min(1.0, t / T)):.5f}"


def window(s: float, e: float, f=0.25) -> str:
    kt = [0.0, s, s + f, e - f, e, T]
    v = [0, 0, 1, 1, 0, 0]
    return (f'<animate attributeName="opacity" dur="{T}s" repeatCount="indefinite" values="{";".join(map(str, v))}" '
            f'keyTimes="{";".join(nk(x) for x in kt)}"/>')


def build() -> str:
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" '
         f'aria-label="CleanDocs AI architecture: data sources, ingestion triggers, 16 Render microservices, AWS orchestration, storage, AI and search, authentication, human review and the planned query API">']
    o.append('<defs>'
             '<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0b1020"/><stop offset="0.6" stop-color="#151a38"/><stop offset="1" stop-color="#22123d"/></linearGradient>'
             '<linearGradient id="panel" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffffff" stop-opacity="0.08"/><stop offset="1" stop-color="#ffffff" stop-opacity="0.025"/></linearGradient>'
             '<marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#a5b4fc"/></marker>'
             '<marker id="ahs" viewBox="0 0 10 10" refX="2" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M10,0 L0,5 L10,10 z" fill="#a5b4fc"/></marker>'
             '<filter id="glow" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>'
             '</defs>')
    o.append(f'<rect width="{W}" height="{H}" rx="18" fill="url(#bg)"/>')
    o.append(f'<text x="30" y="46" font-size="25" font-weight="800" fill="#e0e7ff" {FONT}>CleanDocs AI: architecture, end to end</text>')
    o.append(f'<text x="30" y="70" font-size="12.5" fill="#a5b4fc" {FONT}>sources → ingestion → Render microservices → AWS orchestration, storage, AI and search → (next) query</text>')
    # legend
    o.append('<rect x="1060" y="30" width="34" height="18" rx="5" fill="#a5b4fc" fill-opacity="0.15" stroke="#a5b4fc" stroke-width="2"/>')
    o.append(f'<text x="1102" y="44" font-size="12" fill="#e0e7ff" {FONT}>built and deployed</text>')
    o.append('<rect x="1230" y="30" width="34" height="18" rx="5" fill="#a5b4fc" fill-opacity="0.15" stroke="#a5b4fc" stroke-width="2" stroke-dasharray="5 3"/>')
    o.append(f'<text x="1272" y="44" font-size="12" fill="#e0e7ff" {FONT}>planned</text>')

    # ---------------- columns (panels)
    for x, y, w, h in ((20, 150, 200, 340), (240, 150, 210, 340), (470, 150, 400, 550), (930, 150, 450, 550)):
        body.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="url(#panel)" stroke="#ffffff" stroke-opacity="0.12"/>')
    heading(36, 172, "1  DATA SOURCES", SKY)
    heading(256, 172, "2  HOW IT ARRIVES", TEAL)
    heading(486, 172, "3  RENDER: 16 FASTAPI MICROSERVICES", PURPLE)
    heading(946, 172, "4  AWS", ORANGE)

    # sources
    card("src_app", 30, 180, 180, 54, SKY, "Application", "API / SDK call")
    card("src_man", 30, 260, 180, 54, SKY, "Manual upload", "PowerShell, curl, ZIP")
    card("src_mail", 30, 340, 180, 54, SKY, "Email inbox", "attachments", planned=True)
    card("src_sp", 30, 420, 180, 54, SKY, "SharePoint", "document library", planned=True)
    # triggers
    card("trig_api", 250, 190, 190, 110, TEAL, "Upload API", ["POST /upload with owner and", "Idempotency-Key. Works today."])
    card("trig_evt", 250, 335, 190, 64, TEAL, "Event-driven", "webhook, mail or SharePoint event", planned=True)
    card("trig_cron", 250, 415, 190, 64, TEAL, "Cron pull", "scheduled check of a source", planned=True)
    arrow("M210,207 H250", SKY)
    arrow("M210,287 H250", SKY)
    arrow("M210,367 H250", SKY, dashed=True)
    arrow("M210,447 H250", SKY, dashed=True)
    arrow("M440,242 H485", TEAL)
    arrow("M440,367 H455 V242", TEAL, dashed=True)
    arrow("M440,447 H455 V367", TEAL, dashed=True)

    # Render chips: left column then right column, in pipeline order
    tiers = {"ingest": SKY, "processing": PURPLE, "indexing": TEAL}
    left = [("upload", "ingest"), ("validation", "processing"), ("archive", "ingest"), ("manifest", "processing"),
            ("normalization", "processing"), ("cleaning", "processing"), ("duplicate", "processing"), ("extraction", "processing")]
    right = [("metadata", "processing"), ("enrichment", "processing"), ("chunking", "processing"), ("embedding", "processing"),
             ("vector", "indexing"), ("recon", "indexing"), ("entity", "processing"), ("graph", "processing")]
    names = {"vector": "vector-indexing", "recon": "reconciliation", "entity": "entity-extraction"}
    for col, items in ((485, left), (680, right)):
        for k, (key, tier) in enumerate(items):
            cid = "ch_" + key
            y = 190 + k * 44
            R[cid] = (col, y, 180, 36)
            c = tiers[tier]
            opt = key in ("entity", "graph")
            dash_attr = ' stroke-dasharray="4 3"' if opt else ''
            dash_attr = ' stroke-dasharray="4 3"' if opt else ''
            body.append(f'<rect x="{col}" y="{y}" width="180" height="36" rx="9" fill="{c}" fill-opacity="{0.08 if opt else 0.16}" stroke="{c}" stroke-width="1.6"' + dash_attr + '/>')
            body.append(f'<rect x="{col}" y="{y}" width="6" height="36" rx="3" fill="{c}"/>')
            nm = names.get(key, key) + "-service"
            body.append(f'<text x="{col + 16}" y="{y + 23}" font-size="12" font-weight="600" fill="#ffffff" {FONT}>{nm}</text>')
    body.append(f'<text x="486" y="566" font-size="10.5" fill="#cbd5ff" {FONT}>Left column: ingestion and preparation. Right column: labels, chunks, index.</text>')
    body.append(f'<text x="486" y="581" font-size="10.5" fill="#cbd5ff" {FONT}>Dashed chips (entity, graph) are optional and never block a document.</text>')
    for k, (lab, c) in enumerate((("IAM tier: ingest", SKY), ("processing", PURPLE), ("indexing", TEAL))):
        x = 486 + [0, 130, 225][k]
        body.append(f'<circle cx="{x + 5}" cy="606" r="5" fill="{c}"/><text x="{x + 15}" y="610" font-size="11" fill="#e0e7ff" {FONT}>{lab}</text>')
    body.append(f'<text x="486" y="640" font-size="11" font-weight="700" fill="{YELLOW}" {FONT}>Every endpoint except /health needs Authorization: Bearer secret.</text>')
    body.append(f'<text x="486" y="657" font-size="10.5" fill="#cbd5ff" {FONT}>Docker, Render Free (Frankfurt). Keys come from Render env groups:</text>')
    body.append(f'<text x="486" y="672" font-size="10.5" fill="#cbd5ff" {FONT}>cleandocs-auth, cleandocs-aws-* (one per tier), cleandocs-config.</text>')

    # human review (planned)
    card("human", 250, 505, 190, 150, RED, "Human review", "", planned=True, top=True)
    for k, ln in enumerate(("Confidence below 75:", "file is parked and a", "person is notified by", "Slack, Teams or email.", "Approve or correct,", "then the file resumes.")):
        body.append(f'<text x="262" y="{551 + k * 16}" font-size="10.5" fill="#fecaca" {FONT}>{ln}</text>')
    arrow("M485,530 H440", RED, dashed=True, label="< 75", lx=462, ly=523)
    arrow("M440,548 H485", RED, dashed=True, label="ok", lx=462, ly=563)

    # AWS cards (single column)
    heading(946, 196, "ORCHESTRATION", ORANGE)
    card("eb", 945, 204, 420, 40, ORANGE, "EventBridge schedule + Lambda dispatcher", "every 5 minutes: moves queued files through the gate", size=12.5)
    card("sqs", 945, 250, 420, 40, ORANGE, "SQS queue + DLQ", "one file at a time; retries, then dead-letter queue for replay", size=12.5)
    card("sfn", 945, 296, 420, 40, ORANGE, "Step Functions", "cleandocs-ingestion (upload) and cleandocs-file (each file)", size=12.5)
    heading(946, 366, "STORAGE, STATE AND SECRETS", GREEN)
    card("s3", 945, 374, 420, 40, GREEN, "S3 (private, encrypted, versioned)", "raw / processed / kb-source; originals are write-once", size=12.5)
    card("ddb", 945, 420, 420, 40, GREEN, "DynamoDB", "registry, one-file gate, monthly caps, audit trail", size=12.5)
    card("sec", 945, 466, 420, 40, YELLOW, "Secrets Manager", "EventBridge Connection adds the Bearer header to workflow calls", size=12.5)
    heading(946, 536, "AI AND SEARCH", PINK)
    card("textract", 945, 544, 420, 40, PINK, "Amazon Textract", "OCR for scans and photos, with confidence", size=12.5)
    card("nova", 945, 590, 420, 40, PINK, "Bedrock: Nova Micro", "document type, tags, summary, PII flag", size=12.5)
    card("kb", 945, 636, 205, 52, INDIGO, "Bedrock KB", "Titan V2 embeddings", size=12.5)
    card("s3v", 1160, 636, 205, 52, INDIGO, "S3 Vectors", "vector store", size=12.5)
    # gaps between Render and AWS
    arrow("M945,316 H870", ORANGE, label="calls", lx=907, ly=309)
    arrow("M870,396 H945", GREEN, label="files + state", lx=907, ly=389, both=True)
    arrow("M870,564 H945", PINK, label="OCR", lx=907, ly=557)
    arrow("M870,610 H945", PINK, label="LLM", lx=907, ly=603)
    arrow("M870,662 H945", INDIGO, label="index", lx=907, ly=655)

    # bottom bands
    body.append('<rect x="20" y="715" width="1000" height="130" rx="14" fill="url(#panel)" stroke="#ffffff" stroke-opacity="0.12"/>')
    heading(36, 738, "AUTH AND SECURITY, END TO END", YELLOW)
    cells = (("auth_bearer", "Bearer secret", ["Every call needs it.", "Constant-time compare,", "fails closed, no Swagger."]),
             ("auth_iam", "3 IAM tiers", ["ingest, processing, indexing.", "Least-privilege keys;", "Render never holds admin."]),
             ("auth_sec", "No secret in workflows", ["Secrets Manager behind the", "EventBridge Connection;", "nothing committed to Git."]),
             ("auth_data", "Data protection", ["S3 private, SSE, TLS only,", "originals write-once,", "owner on every chunk."]))
    for k, (cid, t, lines) in enumerate(cells):
        x = 36 + k * 245
        R[cid] = (x - 6, 746, 236, 88)
        body.append(f'<text x="{x}" y="766" font-size="13" font-weight="700" fill="#ffffff" {FONT}>{t}</text>')
        for j, ln in enumerate(lines):
            body.append(f'<text x="{x}" y="{785 + j * 15}" font-size="10.5" fill="#cbd5ff" {FONT}>{ln}</text>')
    card("query", 1040, 715, 340, 130, INDIGO, "Query side (next phase)", "", planned=True, top=True)
    for k, ln in enumerate(("Chat or app asks a question.", "query-api calls the Knowledge Base", "with an owner filter (ADMIN sees all)", "and returns the answer with citations:", "file, page, section.")):
        body.append(f'<text x="1054" y="{762 + k * 16}" font-size="11" fill="#c7d2fe" {FONT}>{ln}</text>')

    # ---------------- animation layer
    o.extend(body)
    for i, (text, ids, dot) in enumerate(STEPS):
        s, e = i * STEP_S, (i + 1) * STEP_S
        for cid in ids:
            x, y, w, h = R[cid]
            o.append(f'<rect x="{x - 3}" y="{y - 3}" width="{w + 6}" height="{h + 6}" rx="12" fill="#ffffff" fill-opacity="0.10" stroke="#ffffff" stroke-width="2.5" opacity="0" filter="url(#glow)">{window(s + 0.05, e - 0.05)}</rect>')
        if dot:
            o.append(f'<circle r="6.5" fill="#fde68a" stroke="#ffffff" stroke-width="1.5" opacity="0" filter="url(#glow)">'
                     f'<animateMotion dur="{T}s" repeatCount="indefinite" path="{dot}" keyPoints="0;0;1;1" keyTimes="0;{nk(s + 0.8)};{nk(e - 0.8)};1" calcMode="linear"/>'
                     f'<animate attributeName="opacity" dur="{T}s" repeatCount="indefinite" values="0;0;1;1;0;0" keyTimes="0;{nk(s + 0.7)};{nk(s + 0.85)};{nk(e - 0.85)};{nk(e - 0.7)};1"/></circle>')
    # caption bar
    o.append('<rect x="20" y="88" width="1360" height="46" rx="12" fill="url(#panel)" stroke="#ffffff" stroke-opacity="0.18"/>')
    for i, (text, ids, dot) in enumerate(STEPS):
        s, e = i * STEP_S, (i + 1) * STEP_S
        o.append(f'<g opacity="0">{window(s, e, 0.3)}<circle cx="52" cy="111" r="16" fill="#fde68a"/>'
                 f'<text x="52" y="117" text-anchor="middle" font-size="16" font-weight="800" fill="#0b1020" {FONT}>{i + 1}</text>'
                 f'<text x="80" y="117" font-size="14.5" font-weight="600" fill="#ffffff" {FONT}>{esc(text)}</text></g>')
    o.append(f'<text x="700" y="868" text-anchor="middle" font-size="11.5" fill="#a5b4fc" {FONT}>Solid = built and deployed. Dashed = planned (sources, cron/event ingestion, human review below 75, query-api).</text>')
    o.append('</svg>')
    return "\n".join(o)


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(build(), encoding="utf-8")
    print("wrote", OUT)
    for i, (t, ids, dot) in enumerate(STEPS, 1):
        print(f"{i}. {t}")
