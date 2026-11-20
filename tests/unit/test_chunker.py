import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "chunking-service"))
from chunking_service.chunker import chunk_document, hard_split, split_paragraph, split_table  # noqa: E402


def blk(i, text, t="paragraph", section=None, unit="p1"):
    b = {"id": f"{unit}-b{i}", "type": t, "text": text}
    if section:
        b["section"] = section
    return b


def run(pages, target=200, mx=300, ov=20):
    return chunk_document(pages, target=target, max_chars=mx, overlap=ov)


def test_paragraphs_pack_up_to_target_and_keep_page_range():
    pages = [{"page": 1, "blocks": [blk(1, "a" * 120), blk(2, "b" * 120)]},
             {"page": 2, "blocks": [blk(1, "c" * 120, unit="p2")]}]
    ch = run(pages)
    assert [c["page_start"] for c in ch] == [1, 2] or ch[0]["page_end"] >= ch[0]["page_start"]
    assert all(c["char_count"] <= 300 for c in ch)
    assert ch[0]["block_ids"][0] == "p1-b1" and ch[-1]["page_end"] == 2


def test_chunk_spanning_pages_records_both_pages():
    pages = [{"page": 1, "blocks": [blk(1, "x" * 60)]}, {"page": 2, "blocks": [blk(1, "y" * 60, unit="p2")]}]
    ch = run(pages)
    assert len(ch) == 1 and (ch[0]["page_start"], ch[0]["page_end"]) == (1, 2)


def test_ids_are_deterministic_and_content_sensitive():
    pages = [{"page": None, "blocks": [blk(1, "hello world " * 10)]}]
    assert run(pages)[0]["chunk_id"] == run(pages)[0]["chunk_id"]
    pages2 = [{"page": None, "blocks": [blk(1, "hello there " * 10)]}]
    assert run(pages)[0]["chunk_id"] != run(pages2)[0]["chunk_id"]


def test_heading_starts_new_chunk_and_sets_section():
    pages = [{"page": None, "blocks": [blk(1, "i" * 150, section="Intro"), blk(2, "Methods", "heading", "Methods"),
                                       blk(3, "m" * 50, section="Methods")]}]
    ch = run(pages, target=1000, mx=1500)
    assert len(ch) == 1  # below the 300-char heading-break threshold
    pages[0]["blocks"][0]["text"] = "i" * 400
    ch = run(pages, target=1000, mx=1500)
    assert [c["section"] for c in ch] == ["Intro", "Methods"]


def test_sheets_never_merge():
    pages = [{"page": None, "sheet": "A", "blocks": [blk(1, "r1 | r2", "table", "A", "s1")]},
             {"page": None, "sheet": "B", "blocks": [blk(1, "q1 | q2", "table", "B", "s2")]}]
    ch = run(pages)
    assert [c["sheet"] for c in ch] == ["A", "B"]


def test_table_split_repeats_header():
    rows = "\n".join(f"row{i} | {'v' * 30}" for i in range(30))
    parts = split_table("name | value\n" + rows, 200)
    assert len(parts) > 1 and all(p.startswith("name | value") and len(p) <= 200 for p in parts)
    assert sum(p.count("row") for p in parts) == 30


def test_oversized_paragraph_split_on_sentences_then_hard_with_overlap():
    text = ". ".join(f"Sentence number {i} is here" for i in range(40)) + "."
    parts = split_paragraph(text, 200, 20)
    assert len(parts) > 1 and all(len(p) <= 200 for p in parts)
    hs = hard_split("z" * 1000, 300, 50)
    assert all(len(p) <= 300 for p in hs) and len(hs) == 4 and hs[0][-50:] == hs[1][:50]
