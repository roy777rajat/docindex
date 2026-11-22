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


def banner() -> str:
    W, H = 1280, 460
    stages = [  # x, colour, title, caption
        (520, "#38bdf8", "Upload", "PDF, scans, Office, ZIP"),
        (655, "#a855f7", "Extract", "unzip + validate"),
        (790, "#f59e0b", "OCR + Clean", "Amazon Textract"),
        (925, "#ec4899", "Enrich", "LLM type, tags, PII"),
        (1060, "#22c55e", "Chunk", "layout-aware"),
        (1195, "#818cf8", "Index", "Knowledge Base"),
    ]
    Y = 235
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" '
         f'aria-label="CleanDocs AI: documents and ZIP files land, are extracted, cleaned, enriched, chunked and indexed">']
    o.append('<defs>'
             '<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0b1020"/><stop offset="0.55" stop-color="#171a3a"/><stop offset="1" stop-color="#2a1646"/></linearGradient>'
             '<linearGradient id="title" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#38bdf8"/><stop offset="0.5" stop-color="#a78bfa"/><stop offset="1" stop-color="#f472b6"/></linearGradient>'
             '<linearGradient id="panel" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffffff" stop-opacity="0.09"/><stop offset="1" stop-color="#ffffff" stop-opacity="0.03"/></linearGradient>'
             '<filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>'
             '</defs>')
    o.append(f'<rect width="{W}" height="{H}" rx="18" fill="url(#bg)"/>')
    # soft background orbs
    for cx, cy, r, c, d in ((760, 90, 150, "#6366f1", 9), (1100, 400, 170, "#ec4899", 11), (480, 420, 120, "#06b6d4", 13)):
        o.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{c}" opacity="0.10"><animate attributeName="r" values="{r};{r + 25};{r}" dur="{d}s" repeatCount="indefinite"/></circle>')
    # ---- left: title and top 5 features
    o.append(f'<rect x="20" y="20" width="420" height="420" rx="16" fill="url(#panel)" stroke="#ffffff" stroke-opacity="0.12"/>')
    o.append(f'<text x="44" y="78" font-size="40" font-weight="800" fill="url(#title)" {FONT}>CleanDocs AI</text>')
    o.append(f'<text x="45" y="104" font-size="14" fill="#c7d2fe" {FONT}>Document ingestion and indexing, built on AWS</text>')
    feats = [("#38bdf8", "Upload anything", "PDF, scans, Word, Excel, text, ZIP"),
             ("#f59e0b", "Reads scans (OCR)", "Amazon Textract, confidence tracked"),
             ("#ec4899", "Labels itself", "LLM doc type, tags, summary, PII flag"),
             ("#22c55e", "Search-ready chunks", "Citations: page, section, owner, date"),
             ("#a78bfa", "Resilient microservices", "16 services, retries, audit trail, reconcile")]
    for i, (c, t, s) in enumerate(feats):
        y = 150 + i * 56
        o.append(f'<g><rect x="44" y="{y - 20}" width="34" height="34" rx="9" fill="{c}"><animate attributeName="opacity" values="0.75;1;0.75" dur="{3 + i * 0.4}s" repeatCount="indefinite"/></rect>'
                 f'<text x="61" y="{y + 3}" text-anchor="middle" font-size="16" font-weight="800" fill="#0b1020" {FONT}>{i + 1}</text>'
                 f'<text x="92" y="{y - 4}" font-size="16" font-weight="700" fill="#ffffff" {FONT}>{t}</text>'
                 f'<text x="92" y="{y + 14}" font-size="12" fill="#c7d2fe" {FONT}>{s}</text></g>')
    # ---- right: pipeline scene
    o.append(f'<text x="860" y="62" text-anchor="middle" font-size="19" font-weight="700" fill="#e0e7ff" {FONT}>Land  →  Extract  →  Clean  →  Enrich  →  Chunk  →  Index</text>')
    o.append(f'<text x="860" y="86" text-anchor="middle" font-size="12.5" fill="#a5b4fc" {FONT}>every file, every ZIP entry, one auditable pipeline</text>')
    # connectors (animated dashes)
    for (x1, *_), (x2, *_) in zip(stages, stages[1:]):
        o.append(f'<line x1="{x1 + 45}" y1="{Y}" x2="{x2 - 45}" y2="{Y}" stroke="#818cf8" stroke-width="2.5" stroke-dasharray="7 6" opacity="0.8">'
                 f'<animate attributeName="stroke-dashoffset" values="26;0" dur="1.2s" repeatCount="indefinite"/></line>')
    # stage boxes
    titles = []
    for i, (x, c, t, cap) in enumerate(stages):
        if t == "Index":
            o.append(f'<g filter="url(#glow)"><ellipse cx="{x}" cy="{Y - 26}" rx="40" ry="11" fill="{c}"/><rect x="{x - 40}" y="{Y - 26}" width="80" height="54" fill="{c}"/>'
                     f'<ellipse cx="{x}" cy="{Y + 28}" rx="40" ry="11" fill="{c}"/><ellipse cx="{x}" cy="{Y - 26}" rx="40" ry="11" fill="#c7d2fe" opacity="0.55"/>'
                     f'<animate attributeName="opacity" values="0.85;1;0.85" dur="2.4s" repeatCount="indefinite"/></g>')
            titles.append(f'<text x="{x}" y="{Y + 8}" text-anchor="middle" font-size="13" font-weight="800" fill="#0b1020" {FONT}>{t}</text>')
        else:
            o.append(f'<g><rect x="{x - 45}" y="{Y - 34}" width="90" height="68" rx="14" fill="{c}" fill-opacity="0.22" stroke="{c}" stroke-width="2">'
                     f'<animate attributeName="fill-opacity" values="0.18;0.45;0.18" dur="{2.4 + i * 0.3}s" begin="{i * 0.35}s" repeatCount="indefinite"/></rect>'
                     f'</g>')
            titles.append(f'<text x="{x}" y="{Y + 5}" text-anchor="middle" font-size="13.5" font-weight="800" fill="#ffffff" stroke="#0b1020" stroke-width="3" paint-order="stroke" {FONT}>{t}</text>')
        o.append(f'<text x="{x}" y="{Y + 64}" text-anchor="middle" font-size="11" fill="#c7d2fe" {FONT}>{cap}</text>')
    # saved badges at the index (pop in repeatedly)
    for k, (dx, dy) in enumerate(((-58, -62), (-10, -74), (40, -62))):
        o.append(f'<g opacity="0"><text x="{1195 + dx}" y="{Y + dy}" font-size="12" font-weight="700" fill="#86efac" {FONT}>✓ saved'
                 f'</text><animate attributeName="opacity" dur="{CYCLE}s" repeatCount="indefinite" values="0;0;1;1;0;0" keyTimes="0;{0.55 + k * 0.1};{0.58 + k * 0.1};{0.7 + k * 0.1};{0.75 + k * 0.1};1"/></g>')
    # travelling documents
    docs = [("#ef4444", "PDF", 120, 0.00), ("#f59e0b", "JPG", 345, 0.10), ("#3b82f6", "DOC", 160, 0.20), ("#22c55e", "XLS", 320, 0.30)]
    for c, lab, y0, a in docs:
        path = f"M 470,{y0} L 520,{Y} L 1195,{Y}"
        o.append(traveller(doc_icon(c, lab), path, a, a + 0.5))
    # a ZIP that lands, opens at Extract and fans out into three files
    o.append(traveller(doc_icon("#a855f7", "ZIP"), f"M 470,260 L 520,{Y} L 655,{Y}", 0.42, 0.56))
    for j, (c, lab, dy) in enumerate((("#ef4444", "PDF", -42), ("#3b82f6", "DOC", 0), ("#22c55e", "XLS", 42))):
        a = 0.56 + j * 0.02
        path = f"M 655,{Y} L 700,{Y + dy} L 1195,{Y}"
        o.append(traveller(doc_icon(c, lab, 0.8), path, a, a + 0.40))
    o.append(f'<text x="655" y="{Y - 54}" text-anchor="middle" font-size="11" font-weight="700" fill="#d8b4fe" {FONT} opacity="0">ZIP opened: files extracted'
             f'<animate attributeName="opacity" dur="{CYCLE}s" repeatCount="indefinite" values="0;0;1;1;0;0" keyTimes="0;0.54;0.57;0.66;0.70;1"/></text>')
    o.extend(titles)
    # footer line
    o.append(f'<text x="860" y="412" text-anchor="middle" font-size="12.5" fill="#a5b4fc" {FONT}>Microservices on Render  ·  AWS Step Functions orchestration  ·  Bedrock Knowledge Base on S3 Vectors</text>')
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
