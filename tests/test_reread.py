from faktura.extract.client import Usage
from faktura.extract.reread import merge, reread, reread_hints, subset_model
from faktura.extract.schema import ExtractedLine, FieldName
from faktura.synth.config import SynthConfig
from faktura.synth.generate import generate
from faktura.validate import Issue, IssueKind
from tests.test_extraction import perfect

INV = generate(SynthConfig(seed=8, n_purchase_orders=3, anomaly_rate=0)).labels[0].invoice

FORMAT = Issue(
    code="npwp_length",
    kind=IssueKind.FORMAT,
    fields=[FieldName.VENDOR_NPWP],
    message="NPWP has 17 digits, expected 16",
)
MASTER = Issue(
    code="bank_master",
    kind=IssueKind.MASTER,
    fields=[FieldName.BANK_ACCOUNT],
    message="bank account differs from vendor master 1234567890",
)


class RecordingClient:
    def __init__(self, answer: dict):
        self.answer = answer
        self.calls = []

    def parse_images(self, images, instructions, user_text, text_format, **kw):
        self.calls.append({"text": user_text, "format": text_format, "n_images": len(images), **kw})
        return text_format(**self.answer), Usage(10, 0, 5), 0.1


def test_subset_model_has_only_requested_fields():
    m = subset_model({FieldName.VENDOR_NPWP, FieldName.LINES})
    assert set(m.model_fields) == {"vendor_npwp", "lines"}


def test_merge_keeps_first_reading_when_second_is_empty():
    ext = perfect(INV)
    sub = subset_model({FieldName.VENDOR_NPWP, FieldName.LINES, FieldName.TOTAL})
    merged = merge(ext, sub(vendor_npwp="1111222233334444", lines=[], total=None))
    assert merged.vendor_npwp == "1111222233334444"
    assert merged.lines == ext.lines
    assert merged.total == ext.total


def test_merge_replaces_lines_when_returned():
    ext = perfect(INV)
    new = [ExtractedLine(description="x", qty=1, unit_price=5, amount=5)]
    merged = merge(ext, subset_model({FieldName.LINES})(lines=new))
    assert merged.lines == new


def test_master_reasons_are_never_shown_to_the_model():
    assert reread_hints([FORMAT, MASTER]) == [FORMAT.message]
    client = RecordingClient({"vendor_npwp": "1", "bank_account": "2"})
    reread(client, b"png", perfect(INV), [FORMAT, MASTER])
    text = client.calls[0]["text"]
    assert "17 digits" in text
    assert "vendor master" not in text and "1234567890" not in text
    assert "bank_account" in text  # the field is still re-read, just without the reason


def test_reread_uses_high_detail_and_more_reasoning():
    client = RecordingClient({"vendor_npwp": "1"})
    reread(client, b"png", perfect(INV), [FORMAT])
    assert client.calls[0]["detail"] == "high"
    assert client.calls[0]["reasoning"] == "medium"


def test_tiles_cover_the_page_and_are_sent_after_it():
    import io

    from PIL import Image

    from faktura.extract.reread import make_tiles

    buf = io.BytesIO()
    Image.new("RGB", (600, 900), "white").save(buf, format="PNG")
    tiles = [Image.open(io.BytesIO(t)) for t in make_tiles(buf.getvalue())]
    assert len(tiles) == 6
    assert all(t.width < 600 and t.height < 900 for t in tiles)
    # overlapping 2x3 grid: total area exceeds the page
    assert sum(t.width * t.height for t in tiles) > 600 * 900

    client = RecordingClient({"vendor_npwp": "1"})
    reread(client, buf.getvalue(), perfect(INV), [FORMAT], tiles=True)
    assert client.calls[0]["n_images"] == 7


def test_no_issues_means_no_call():
    client = RecordingClient({})
    ext, fields, _, _ = reread(client, b"png", perfect(INV), [])
    assert fields == set() and not client.calls and ext == perfect(INV)
