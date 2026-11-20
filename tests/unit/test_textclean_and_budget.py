from cleandocs_shared.registry import Registry
from cleandocs_shared.textclean import clean_text, protected_tokens


def test_clean_text_whitespace_controls_and_wraps():
    assert clean_text("a  b\t c\x00\r\n\r\n\r\n\r\nd") == "a b c\n\nd"
    assert clean_text("inter-\nnational trade", join_lines=True) == "international trade"
    assert clean_text("line one\nline two", join_lines=True) == "line one line two"
    assert clean_text("r1\nr2", join_lines=False) == "r1\nr2"


def test_numbers_dates_and_currency_survive_cleaning():
    src = "Total ₹1,234.50 due 12/03/2025 (12-3) at 10:30, 5%\nref 2024-\n01"
    assert protected_tokens(clean_text(src, join_lines=True)) == protected_tokens(src)


def test_claim_checksum_first_wins_and_is_idempotent(registry_table):
    r = Registry(registry_table)
    assert r.claim_checksum("c" * 64, "doc_a") == "doc_a"
    assert r.claim_checksum("c" * 64, "doc_b") == "doc_a"
    assert r.claim_checksum("c" * 64, "doc_a") == "doc_a"


def test_reserve_counter_enforces_cap(registry_table):
    r = Registry(registry_table)
    assert r.reserve_counter("2026-11", "textract_pages", 200, 300)
    assert r.reserve_counter("2026-11", "textract_pages", 100, 300)
    assert not r.reserve_counter("2026-11", "textract_pages", 1, 300)
    assert not r.reserve_counter("2026-12", "textract_pages", 301, 300)
    assert r.reserve_counter("2026-12", "textract_pages", 300, 300)
