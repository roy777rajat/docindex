"""Build build/dispatcher.zip: the handler plus the three shared modules it needs (boto3 comes with the Lambda runtime).

Usage: python scripts/package_dispatcher.py   -> prints the zip path and its SHA-256 prefix (used in the S3 key).
"""
import hashlib
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "handler.py": ROOT / "workers" / "sqs-dispatcher" / "handler.py",
    "cleandocs_shared/__init__.py": ROOT / "shared" / "cleandocs_shared" / "__init__.py",
    "cleandocs_shared/gate.py": ROOT / "shared" / "cleandocs_shared" / "gate.py",
    "cleandocs_shared/utils.py": ROOT / "shared" / "cleandocs_shared" / "utils.py",
}


def build(out_dir: Path | None = None) -> Path:
    out = (out_dir or ROOT / "build")
    out.mkdir(parents=True, exist_ok=True)
    path = out / "dispatcher.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, src in FILES.items():
            info = zipfile.ZipInfo(arc, date_time=(2020, 1, 1, 0, 0, 0))  # fixed timestamp: same code -> same hash
            info.external_attr = 0o644 << 16
            z.writestr(info, src.read_bytes())
    return path


if __name__ == "__main__":
    p = build()
    print(p)
    print(hashlib.sha256(p.read_bytes()).hexdigest()[:12])
    sys.exit(0)
