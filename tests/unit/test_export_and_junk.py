"""Export loading (unwrapping, ordering, selection) and the deterministic junk filter."""

from src.attachments.junk_filter import attachment_name, decide, extension, family_of
from src.corpus.export import load_id_set, unwrap
from tests.conftest import FAKE_PDF, PNG_1x1


def test_unwrap_oid_and_date():
    assert unwrap({"$oid": "abc"}) == "abc"
    assert unwrap({"a": {"$date": "2026-01-01T00:00:00Z"}, "b": [{"$oid": "x"}]}) == {"a": "2026-01-01T00:00:00Z", "b": ["x"]}
    assert unwrap({"$oid": "a", "other": 1}) == {"$oid": "a", "other": 1}


def test_export_indexes_and_sorts_pages(mini_export, mini_casefile):
    assert len(mini_export) == 1
    assert "aaaaaaaaaaaaaaaaaaaaaaa1" in mini_export
    cf = mini_casefile
    assert cf.imo == 1234567
    assert cf.title == "Main engine OMD sensor replacement process"
    assert [p.pagenum for p in cf.pages] == [0, 1, 2]  # fixture stores them out of order
    assert cf.pages[0].mail_id == "bbbbbbbbbbbbbbbbbbbbbbb1"
    assert cf.pages[1].attachments == [
        "https://example.invalid/att/AAA/image001.png",
        "https://example.invalid/att/AAA/OMD%20service%20note.pdf",
    ]
    assert cf.attachment_link_count == 2
    meta = cf.export_meta()
    assert meta["UICategory"] == ["defect"] and meta["tags"] == ["defect", "maintenance"]
    assert "summary" not in meta  # the LLM-written export fields never reach the record


def test_export_select_reports_missing(mini_export):
    found, missing = mini_export.select(["aaaaaaaaaaaaaaaaaaaaaaa1", "ffffffffffffffffffffffff"])
    assert [c.casefile_id for c in found] == ["aaaaaaaaaaaaaaaaaaaaaaa1"]
    assert missing == ["ffffffffffffffffffffffff"]


def test_load_id_set_accepts_both_shapes(tmp_path):
    p = tmp_path / "set.json"
    p.write_text('{"casefiles": [{"casefile_id": "a", "label": "A"}, "b"]}', encoding="utf-8")
    assert load_id_set(p) == [{"casefile_id": "a", "label": "A"}, {"casefile_id": "b", "label": ""}]
    p.write_text('["x", "y"]', encoding="utf-8")
    assert [i["casefile_id"] for i in load_id_set(p)] == ["x", "y"]


def test_attachment_name_and_extension():
    assert attachment_name("https://h/Mail%20Attachment/ID/OMD%20service%20note.pdf") == "OMD service note.pdf"
    assert extension("sysLog_1.tar.gz") == ".tar.gz"
    assert extension("dump.manbw-spaf") == ".manbw-spaf"
    assert extension("noext") == ""


def test_family_of():
    assert family_of("a.PDF") == "pdf"
    assert family_of("a.docx") == "word"
    assert family_of("a.xlsm") == "sheet"
    assert family_of("fwd.eml") == "eml"
    assert family_of("x.zip") == "archive"
    assert family_of("setting.addr") == "telemetry"
    assert family_of("photo.JPG") == "image"
    assert family_of("blob", "image/png") == "image"


def test_junk_filter_drops_signature_images_and_keeps_documents():
    d = decide("image001.png", b"x" * 50000)
    assert not d.content_bearing and "signature/inline" in d.reason
    d = decide("image013.png", b"x" * 650000)  # inline name, but 650 KB: a pasted photo
    assert d.content_bearing and "pasted photo" in d.reason
    d = decide("photo.jpg", PNG_1x1)  # real name but tiny bytes
    assert not d.content_bearing and "under" in d.reason
    d = decide("big_photo.jpg", b"\xff" * 20000)  # unreadable as image -> size rule only
    assert d.content_bearing
    assert decide("report.pdf", FAKE_PDF).content_bearing
    assert not decide("invite.ics", b"BEGIN:VCALENDAR").content_bearing
    assert decide("dump.zip", b"PK\x03\x04" + b"0" * 100).content_bearing  # archives are listed facts
    assert not decide("CID_12345", b"x" * 100).content_bearing
