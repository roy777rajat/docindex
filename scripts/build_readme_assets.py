"""Generate the animated README banner and the technology-stack image (pure SVG, no external files).
   python scripts/build_readme_assets.py     -> docs/assets/banner.svg, docs/assets/tech-stack.svg"""
from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docs" / "assets"
CYCLE = 12  # seconds for one animation loop
FONT = "font-family=\"'Segoe UI',Roboto,Helvetica,Arial,sans-serif\""


def doc_icon(color: str, label: str, scale: float = 1.0) -> str:
    return (f'<g transform="scale({scale})"><rect x="-15" y="-19" width="30" height="38" rx="4" fill="{color}"/>'
            f'<polygon points="5,-19 15,-9 5,-9" fill="#ffffff" fill-opacity="0.45"/>'
            f'<rect x="-9" y="-3" width="18" height="2" rx="1" fill="#fff" fill-opacity="0.55"/>'
            f'<rect x="-9" y="3" width="13" height="2" rx="1" fill="#fff" fill-opacity="0.55"/>'
            f'<text x="0" y="-11" text-anchor="middle" font-size="7.5" font-weight="700" fill="#fff" {FONT}>{label}</text></g>')


def traveller(icon: str, path: str, a: float, b: float) -> str:
    """One element that waits invisible until `a` (fraction of the cycle), travels the path until `b`, then fades."""
    kt = f"0;{a};{b};1"
    return (f'<g opacity="0">{icon}'
            f'<animateMotion dur="{CYCLE}s" repeatCount="indefinite" path="{path}" keyPoints="0;0;1;1" keyTimes="{kt}" calcMode="linear"/>'
            f'<animate attributeName="opacity" dur="{CYCLE}s" repeatCount="indefinite" '
            f'values="0;0;1;1;0;0" keyTimes="0;{a};{min(a + 0.03, b)};{max(b - 0.03, a + 0.03)};{b};1"/></g>')


# ------------------------------------------------------------------ banner
STAGES = [  # title, caption, colour, panel lines (single file)
    ("Upload", "S3 original", "#38bdf8", ["Upload service stores the untouched original in S3",
        "Owner and Idempotency-Key are recorded, so a retry is safe",
        "Starts the Step Functions ingestion workflow"]),
    ("Checksum", "SHA-256", "#2dd4bf", ["SHA-256 is computed and verified by S3 on write",
        "Exact duplicates are detected: skip, link or reprocess",
        "The original is immutable: written once, never overwritten"]),
    ("Validate", "magic bytes", "#84cc16", ["File type is read from the content (magic bytes), not the extension",
        "Size, page and image-pixel limits are enforced",
        "Bad files are rejected early and safely"]),
    ("Archive", "unzip safely", "#a855f7", ["Not a ZIP, so the file passes straight through",
        "ZIPs only: zip-bomb and path checks, each entry becomes its own document",
        "Children inherit the owner of the upload"]),
    ("SQS", "work queue", "#fb923c", ["The file waits in the SQS queue",
        "A dispatcher feeds one file at a time through the DynamoDB gate",
        "Failed steps retry, then the message moves to the DLQ"]),
    ("Normalize", "clean JSON", "#60a5fa", ["PDF, Word, Excel, CSV and images become uniform page/block JSON",
        "Cleaning fixes whitespace, hyphenation and control characters",
        "Pages that need OCR are marked"]),
    ("Extract", "OCR + text", "#f59e0b", ["Real text is kept; scanned pages go to Amazon Textract (OCR)",
        "OCR confidence is stored; a monthly page cap bounds the spend",
        "Rule-based title, type guess, dates and keywords are added"]),
    ("Enrich", "LLM labels", "#ec4899", ["One Nova Micro call returns type, tags, summary, language, date, PII flag",
        "The answer is validated against a closed vocabulary (injection-safe)",
        "Soft stage: it can never fail a document"]),
    ("Chunk", "layout-aware", "#22c55e", ["Layout-aware split: headings, tables with header rows, sentence-aware paragraphs",
        "Overlap plus locators (page, section, sheet) for citations",
        "Chunk ids come from the text hash, so re-ingest creates no duplicates"]),
    ("Embed", "+ metadata", "#818cf8", ["Chunk file plus a metadata sidecar are written for Bedrock",
        "owner, doc_type, tags, date, language become search filters",
        "A document becomes visible only when it is complete"]),
    ("Vectorize", "Bedrock KB", "#c084fc", ["A Bedrock ingestion job embeds the chunks with Titan Text V2",
        "Vectors are stored in S3 Vectors (serverless, no idle cost)",
        "Reconciliation re-checks S3 and the Knowledge Base before COMPLETED"]),
]
SERVICES = [("upload", "service"), ("duplicate", "service"), ("validation", "service"), ("archive", "service"),
            ("SQS + Lambda", "dispatcher"), ("normalization", "service"), ("extraction", "service"), ("enrichment", "service"),
            ("chunking", "service"), ("embedding", "service"), ("vector-indexing", "service")]
SVC_NAMES = ["upload-service", "duplicate-service (+ SHA-256 in upload)", "validation-service", "archive-service",
             "SQS queue + sqs-dispatcher Lambda", "normalization-service (+ cleaning)", "extraction-service", "enrichment-service",
             "chunking-service", "embedding-service", "vector-indexing-service (+ reconciliation)"]
N = len(STAGES)
BX0, BSTEP, BY, BW, BH, LANE = 495, 85, 170, 70, 62, 112
T = 111.0


def sx(i: int) -> float:
    return BX0 + BSTEP * i


def nk(t: float) -> str:
    return f"{max(0.0, min(1.0, t / T)):.5f}"


def window_anim(windows, f=0.2) -> str:
    kt, v = [0.0], [0]
    for s, e in windows:
        ff = min(f, (e - s) / 2.2)
        kt += [s, s + ff, e - ff, e]
        v += [0, 1, 1, 0]
    kt.append(T)
    v.append(0)
    return (f'<animate attributeName="opacity" dur="{T}s" repeatCount="indefinite" '
            f'values="{";".join(map(str, v))}" keyTimes="{";".join(nk(x) for x in kt)}"/>')


def mover(icon: str, evs, xs: float, xe: float, fade_end: float) -> str:
    """evs = [(arrive, leave, x)...]: the icon sits at x from arrive to leave, glides to the next x."""
    L = xe - xs
    kt, kp = [0.0], [(evs[0][2] - xs) / L]
    for a, l, x in evs:
        kt += [a, l]
        kp += [(x - xs) / L] * 2
    kt.append(T)
    kp.append(kp[-1])
    s = evs[0][0]
    op_kt = [0.0, s, s + 0.25, fade_end, fade_end + 0.3, T]
    return (f'<g opacity="0">{icon}'
            f'<animateMotion dur="{T}s" repeatCount="indefinite" path="M {xs},{LANE} L {xe},{LANE}" '
            f'keyPoints="{";".join(f"{p:.5f}" for p in kp)}" keyTimes="{";".join(nk(x) for x in kt)}" calcMode="linear"/>'
            f'<animate attributeName="opacity" dur="{T}s" repeatCount="indefinite" values="0;0;1;1;0;0" '
            f'keyTimes="{";".join(nk(x) for x in op_kt)}"/></g>')


def banner() -> str:
    W, H = 1400, 520
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" '
         f'aria-label="CleanDocs AI: a single file, then a ZIP, move step by step through upload, checksum, validate, archive, SQS, normalize, extract, enrich, chunk, embed and vectorize; failures go to the DLQ">']
    o.append('<defs>'
             '<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0b1020"/><stop offset="0.55" stop-color="#171a3a"/><stop offset="1" stop-color="#2a1646"/></linearGradient>'
             '<linearGradient id="title" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#38bdf8"/><stop offset="0.5" stop-color="#a78bfa"/><stop offset="1" stop-color="#f472b6"/></linearGradient>'
             '<linearGradient id="panel" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffffff" stop-opacity="0.09"/><stop offset="1" stop-color="#ffffff" stop-opacity="0.03"/></linearGradient>'
             '<filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>'
             '</defs>')
    o.append(f'<rect width="{W}" height="{H}" rx="18" fill="url(#bg)"/>')
    for cx, cy, r, c, d in ((800, 90, 150, "#6366f1", 9), (1250, 440, 170, "#ec4899", 11), (500, 470, 120, "#06b6d4", 13)):
        o.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{c}" opacity="0.10"><animate attributeName="r" values="{r};{r + 25};{r}" dur="{d}s" repeatCount="indefinite"/></circle>')
    # ---- left: title and top 5 features
    o.append('<rect x="20" y="20" width="420" height="480" rx="16" fill="url(#panel)" stroke="#ffffff" stroke-opacity="0.12"/>')
    o.append(f'<text x="44" y="82" font-size="40" font-weight="800" fill="url(#title)" {FONT}>CleanDocs AI</text>')
    o.append(f'<text x="45" y="110" font-size="14" fill="#c7d2fe" {FONT}>Document ingestion and indexing, built on AWS</text>')
    feats = [("#38bdf8", "Upload anything", "PDF, scans, Word, Excel, text, ZIP"),
             ("#f59e0b", "Reads scans (OCR)", "Amazon Textract, confidence tracked"),
             ("#ec4899", "Labels itself", "LLM doc type, tags, summary, PII flag"),
             ("#22c55e", "Search-ready chunks", "Citations: page, section, owner, date"),
             ("#a78bfa", "Resilient microservices", "16 services, SQS + DLQ, retries, audit trail")]
    for i, (c, t, s) in enumerate(feats):
        y = 168 + i * 64
        o.append(f'<g><rect x="44" y="{y - 20}" width="34" height="34" rx="9" fill="{c}"><animate attributeName="opacity" values="0.75;1;0.75" dur="{3 + i * 0.4}s" repeatCount="indefinite"/></rect>'
                 f'<text x="61" y="{y + 3}" text-anchor="middle" font-size="16" font-weight="800" fill="#0b1020" {FONT}>{i + 1}</text>'
                 f'<text x="92" y="{y - 4}" font-size="16" font-weight="700" fill="#ffffff" {FONT}>{t}</text>'
                 f'<text x="92" y="{y + 14}" font-size="12" fill="#c7d2fe" {FONT}>{s}</text></g>')
    # ---- right: heading
    o.append(f'<text x="920" y="50" text-anchor="middle" font-size="19" font-weight="700" fill="#e0e7ff" {FONT}>From upload to searchable index, step by step</text>')
    o.append(f'<text x="920" y="74" text-anchor="middle" font-size="12.5" fill="#a5b4fc" {FONT}>one file, then a ZIP, then what happens when a step fails</text>')

    # ---- schedule (seconds)
    step_s, dw_s = 5.0, 4.4
    a_s = [1.0 + step_s * i for i in range(N)]                       # single file arrivals
    Z0, zstep, zdw = 58.0, 3.0, 2.4
    a_z = [Z0 + zstep * i for i in range(4)]                        # zip: upload, checksum, validate, archive
    open_t = a_z[3] + 1.5
    dep = [open_t + 3.0 + 0.5 * j for j in range(3)]                # children leave the archive box
    cstep, cdw = 4.0, 3.5
    a_c = [dep[0] + 0.5 + cstep * (i - 4) for i in range(4, N)]    # lead child arrivals at stage 4..10
    D0 = 101.8

    # ---- connectors and boxes
    for i in range(N - 1):
        o.append(f'<line x1="{sx(i) + BW / 2}" y1="{BY}" x2="{sx(i + 1) - BW / 2}" y2="{BY}" stroke="#818cf8" stroke-width="2.5" stroke-dasharray="5 4" opacity="0.85">'
                 f'<animate attributeName="stroke-dashoffset" values="18;0" dur="1.2s" repeatCount="indefinite"/></line>')
    hi = {i: [(a_s[i], a_s[i] + dw_s + 0.2)] for i in range(N)}
    for i in range(4):
        hi[i].append((a_z[i], (dep[0] + 0.2) if i == 3 else a_z[i] + zdw + 0.15))
    for k, i in enumerate(range(4, N)):
        hi[i].append((a_c[k], a_c[k] + cdw + 0.15))
    titles = []
    for i, (t, cap, c, _) in enumerate(STAGES):
        x = sx(i)
        if t == "Vectorize":
            o.append(f'<g><ellipse cx="{x}" cy="{BY - 24}" rx="35" ry="10" fill="{c}" fill-opacity="0.35" stroke="{c}" stroke-width="2"/>'
                     f'<rect x="{x - 35}" y="{BY - 24}" width="70" height="50" fill="{c}" fill-opacity="0.28"/>'
                     f'<line x1="{x - 35}" y1="{BY - 24}" x2="{x - 35}" y2="{BY + 26}" stroke="{c}" stroke-width="2"/><line x1="{x + 35}" y1="{BY - 24}" x2="{x + 35}" y2="{BY + 26}" stroke="{c}" stroke-width="2"/>'
                     f'<ellipse cx="{x}" cy="{BY + 26}" rx="35" ry="10" fill="{c}" fill-opacity="0.35" stroke="{c}" stroke-width="2"/></g>')
        else:
            o.append(f'<rect x="{x - BW / 2}" y="{BY - BH / 2}" width="{BW}" height="{BH}" rx="13" fill="{c}" fill-opacity="0.18" stroke="{c}" stroke-width="2"/>')
        # highlight overlay while a file is inside
        o.append(f'<rect x="{x - BW / 2 - 3}" y="{BY - BH / 2 - 3}" width="{BW + 6}" height="{BH + 6}" rx="15" fill="{c}" fill-opacity="0.30" stroke="#ffffff" stroke-width="2" opacity="0" filter="url(#glow)">{window_anim(hi[i], 0.2)}</rect>')
        titles.append(f'<text x="{x}" y="{BY + (0 if t != "Vectorize" else 2)}" text-anchor="middle" font-size="13" font-weight="800" fill="#ffffff" {FONT}>{t}</text>'
                      f'<text x="{x}" y="{BY + 17}" text-anchor="middle" font-size="9.5" fill="#dbe4ff" {FONT}>{cap}</text>'
                      f'<text x="{x}" y="{BY - 14}" text-anchor="middle" font-size="9.5" font-weight="700" fill="{c}" {FONT}>{i + 1}</text>')
    for i, ((l1, l2), st) in enumerate(zip(SERVICES, STAGES)):
        o.append(f'<text x="{sx(i)}" y="{BY + BH / 2 + 15}" text-anchor="middle" font-size="10" font-weight="700" fill="{st[2]}" {FONT}>{l1}</text>'
                 f'<text x="{sx(i)}" y="{BY + BH / 2 + 27}" text-anchor="middle" font-size="10" fill="{st[2]}" {FONT}>{l2}</text>')
    # DLQ box under SQS
    qx = sx(4)
    o.append(f'<line x1="{qx}" y1="{BY + BH / 2 + 31}" x2="{qx}" y2="{BY + BH / 2 + 50}" stroke="#f87171" stroke-width="2" stroke-dasharray="4 4"/>')
    o.append(f'<text x="{qx + 24}" y="{BY + BH / 2 + 45}" font-size="9.5" fill="#fca5a5" {FONT}>after retries</text>')
    o.append(f'<rect x="{qx - 35}" y="{BY + BH / 2 + 50}" width="70" height="34" rx="11" fill="#ef4444" fill-opacity="0.18" stroke="#ef4444" stroke-width="2"/>')
    o.append(f'<rect x="{qx - 38}" y="{BY + BH / 2 + 47}" width="76" height="40" rx="13" fill="#ef4444" fill-opacity="0.30" stroke="#ffffff" stroke-width="2" opacity="0" filter="url(#glow)">{window_anim([(D0 + 3.4, D0 + 8.2)])}</rect>')
    o.append(f'<text x="{qx}" y="{BY + BH / 2 + 72}" text-anchor="middle" font-size="13" font-weight="800" fill="#ffffff" {FONT}>DLQ</text>')
    o.extend(titles)

    # ---- detail panel
    PX, PY, PW, PH = 460, 312, 920, 160
    o.append(f'<rect x="{PX}" y="{PY}" width="{PW}" height="{PH}" rx="14" fill="url(#panel)" stroke="#ffffff" stroke-opacity="0.15"/>')

    def pgroup(windows, color, badge, title, lines, size=15, svc="") -> str:
        g = [f'<g opacity="0">{window_anim(windows)}',
             (f'<text x="{PX + PW - 20}" y="{PY + 56}" text-anchor="end" font-size="14" font-weight="700" fill="{color}" {FONT}>service: {svc}</text>' if svc else ''),
             f'<circle cx="{PX + 38}" cy="{PY + 42}" r="20" fill="{color}"/>'
             f'<text x="{PX + 38}" y="{PY + 48}" text-anchor="middle" font-size="18" font-weight="800" fill="#0b1020" {FONT}>{badge}</text>'
             f'<text x="{PX + 72}" y="{PY + 50}" font-size="23" font-weight="800" fill="#ffffff" {FONT}>{title}</text>']
        for k, ln in enumerate(lines):
            g.append(f'<circle cx="{PX + 30}" cy="{PY + 84 + k * 28 - 5}" r="3.5" fill="{color}"/>'
                     f'<text x="{PX + 44}" y="{PY + 84 + k * 28}" font-size="{size}" fill="#e0e7ff" {FONT}>{ln}</text>')
        g.append('</g>')
        return "".join(g)

    # single file: one group per stage (Archive uses the "single" wording = line 1 only plus note)
    for i, (t, cap, c, lines) in enumerate(STAGES):
        end = a_s[i + 1] if i < N - 1 else a_s[i] + dw_s + 1.2
        ls = lines if t != "Archive" else [lines[0], "ZIP uploads take a different route (see the next scene)", "Each file is tracked as its own document"]
        o.append(pgroup([(a_s[i] + 0.05, end + 0.05)], c, str(i + 1), t, ls, svc=SVC_NAMES[i]))
    # ZIP scene
    o.append(pgroup([(Z0 - 0.2, a_z[3])], "#a855f7", "ZIP", "One ZIP upload", svc="upload, duplicate, validation services",
                    lines=["The whole ZIP is stored once as the untouched original", "Checksum and validation run on the ZIP itself, once",
                     "Next: Archive opens it"]))
    o.append(pgroup([(a_z[3], dep[0] + 0.2)], "#a855f7", "4", "Archive opens the ZIP", svc=SVC_NAMES[3], lines=
                    ["Zip-bomb limits and path checks run before anything is extracted",
                     "Every entry becomes its own document with its own status",
                     "All children inherit the owner of the upload"]))
    for k, i in enumerate(range(4, N)):
        end = a_c[k + 1] if k < len(a_c) - 1 else a_c[k] + cdw + 1.5
        t, cap, c, lines = STAGES[i]
        o.append(pgroup([(a_c[k] - 0.1, end)], c, str(i + 1), "3 files from the ZIP, one by one", svc=SVC_NAMES[i],
                        lines=[f"Now at step {i + 1}, {t}: {lines[0]}", "Same steps as the single file: each gets its own chunks, labels and status",
                         "The SQS gate feeds them through one at a time, so cost and memory stay small"], size=14.5))
    # failure scene
    o.append(pgroup([(D0, D0 + 8.6)], "#ef4444", "!", "When a step fails: SQS retries, then the DLQ", svc="SQS queue + DLQ",
                    lines=["The queue retries the step automatically with back-off", "After the last retry the message moves to the DLQ and is kept, not lost",
                     "Fix the cause, then replay the file from the DLQ: nothing disappears silently"]))
    # mode tag (top right of panel)
    for label, win, col in (("SINGLE FILE", (0.3, Z0 - 1.6), "#38bdf8"), ("ZIP UPLOAD", (Z0 - 1.3, D0 - 0.6), "#a855f7"), ("WHEN SOMETHING FAILS", (D0 - 0.2, D0 + 8.6), "#ef4444")):
        o.append(f'<text x="{PX + PW - 20}" y="{PY + 30}" text-anchor="end" font-size="12" font-weight="800" fill="{col}" letter-spacing="1.5" opacity="0" {FONT}>{label}{window_anim([win])}</text>')

    # ---- travellers
    pdf = doc_icon("#ef4444", "PDF", 0.9)
    ev = [(0.5, 0.5, sx(0) - 55)] + [(a_s[i], a_s[i] + dw_s, sx(i)) for i in range(N)]
    o.append(mover(pdf, ev, sx(0) - 55, sx(N - 1), a_s[-1] + dw_s + 0.3))
    # ZIP: enters, passes upload/checksum/validate, opens at archive
    zev = [(Z0 - 0.6, Z0 - 0.6, sx(0) - 55)] + [(a_z[i], a_z[i] + (1.5 if i == 3 else zdw), sx(i)) for i in range(4)]
    o.append(mover(doc_icon("#a855f7", "ZIP", 0.9), zev, sx(0) - 55, sx(3), a_z[3] + 1.5))
    # three children leave the archive box and travel one behind another
    kids = (("#ef4444", "PDF", -26), ("#3b82f6", "DOC", 0), ("#22c55e", "XLS", 26))
    for j, (c, lab, off) in enumerate(kids):
        cev = [(open_t, dep[j], sx(3) + off)]
        for k, i in enumerate(range(4, N)):
            cev.append((a_c[k] + 0.5 * j, a_c[k] + 0.5 * j + cdw, sx(i)))
        o.append(mover(doc_icon(c, lab, 0.85), cev, sx(3) - 26, sx(N - 1), cev[-1][1] + 0.2))
    # failed file drops from SQS into the DLQ
    fx = sx(4)
    drop = (f'<g opacity="0">{doc_icon("#ef4444", "!", 0.9)}'
            f'<animateMotion dur="{T}s" repeatCount="indefinite" path="M {fx},{LANE} L {fx},{BY + BH / 2 + 67}" keyPoints="0;0;1;1" '
            f'keyTimes="0;{nk(D0 + 1.2)};{nk(D0 + 3.4)};1" calcMode="linear"/>'
            f'<animate attributeName="opacity" dur="{T}s" repeatCount="indefinite" values="0;0;1;1;0;0" '
            f'keyTimes="0;{nk(D0 + 0.6)};{nk(D0 + 0.9)};{nk(D0 + 8.0)};{nk(D0 + 8.5)};1"/></g>')
    o.append(drop)
    o.append(f'<text x="920" y="500" text-anchor="middle" font-size="12.5" fill="#a5b4fc" {FONT}>Microservices on Render  ·  AWS Step Functions + SQS orchestration  ·  Bedrock Knowledge Base on S3 Vectors</text>')
    o.append('</svg>')
    return "\n".join(o)


def tech() -> str:
    groups = [
        ("Application", "#38bdf8", ["Python 3.12", "FastAPI", "Uvicorn", "Pydantic", "boto3", "Docker", "pypdf", "python-docx", "openpyxl", "Pillow"]),
        ("AWS", "#f59e0b", ["S3", "S3 Vectors", "Bedrock Knowledge Bases", "Titan Text Embeddings V2", "Amazon Nova Micro", "Textract", "Step Functions",
                            "Lambda", "EventBridge", "SQS + DLQ", "DynamoDB", "IAM", "CloudFormation", "CloudWatch Logs", "Secrets Manager"]),
        ("Platform and tooling", "#a78bfa", ["Render (Free, Docker)", "GitHub", "PowerShell", "uv", "pytest", "moto"]),
    ]
    W = 1280
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} 1" width="{W}" role="img" aria-label="Technology stack">']
    body, y = [], 24
    body.append(f'<text x="30" y="{y + 18}" font-size="22" font-weight="800" fill="#e0e7ff" {FONT}>Technology stack</text>')
    y += 46
    for title, color, items in groups:
        body.append(f'<text x="30" y="{y + 14}" font-size="14" font-weight="700" fill="{color}" {FONT}>{title.upper()}</text>')
        y += 26
        x = 30
        for it in items:
            w = int(len(it) * 7.9 + 48)
            if x + w > W - 30:
                x, y = 30, y + 42
            body.append(f'<g><rect x="{x}" y="{y}" width="{w}" height="32" rx="16" fill="{color}" fill-opacity="0.16" stroke="{color}" stroke-width="1.5"/>'
                        f'<circle cx="{x + 16}" cy="{y + 16}" r="4.5" fill="{color}"/>'
                        f'<text x="{x + 28}" y="{y + 21}" font-size="13" font-weight="600" fill="#f1f5f9" {FONT}>{it}</text></g>')
            x += w + 10
        y += 56
    H = y + 6
    o[0] = o[0].replace('viewBox="0 0 1280 1"', f'viewBox="0 0 {W} {H}"') + f'<rect width="{W}" height="{H}" rx="18" fill="#0f1330"/>'
    return "\n".join(o + body + ["</svg>"]).replace(f'width="{W}" role', f'width="{W}" height="{H}" role', 1)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "banner.svg").write_text(banner(), encoding="utf-8")
    (OUT / "tech-stack.svg").write_text(tech(), encoding="utf-8")
    print("wrote", OUT / "banner.svg", OUT / "tech-stack.svg")
