"""Each check is shown failing on a bad draft and passing on a good one."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from checks.outbound_checks import run_all  # noqa: E402


def good_draft(tmp_path):
    pdf = tmp_path / "quote.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    return {
        "to": ["buyer@example.gov"],
        "subject": "Quote 1042",
        "body": "Hi Jane, the quote is attached. Let me know if you need anything else.",
        "attachments": [str(pdf)],
    }


def test_good_draft_passes(tmp_path):
    assert run_all(good_draft(tmp_path)) == []


def test_each_rule_blocks(tmp_path):
    cases = {
        "no recipient": {"to": []},
        "empty subject": {"subject": "  "},
        "unfilled placeholder": {"body": "Hi {{first_name}}, attached."},
        "em dash": {"body": "Attached \u2014 thanks."},
        "attachment not found": {"attachments": [str(tmp_path / "missing.pdf")]},
        "mentions an attachment": {"attachments": []},
    }
    for expected, change in cases.items():
        draft = {**good_draft(tmp_path), **change}
        failures = run_all(draft)
        assert any(expected in f for f in failures), (expected, failures)


def test_todo_marker_blocks(tmp_path):
    assert run_all({**good_draft(tmp_path), "body": "Price is [TBD], see attached."})
