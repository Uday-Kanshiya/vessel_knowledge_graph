"""
Byte sniffing (eml labelled .pdf), identity-document rule, OCR of scanned PDF
pages through the vision backend, and the non-English quote translation rule
(store 'original [EN: translation]', validate on the original).
"""

import json

import pytest

from src.attachments.digest import AttachmentDigester
from src.attachments.fetch import Fetcher
from src.attachments.ocr import PdfOcr
from src.attachments.readers import READER_VERSION, is_identity_document, read_bytes, sniff_family
from src.contract.validator import validate_record
from src.extraction.record import apply_delta, new_record, strip_sidecars
from src.extraction.translate import (
    needs_translation,
    normalise_quote,
    quotes_missing_translation,
    split_quote,
    strip_translation,
    translate_missing,
)
from src.utils.token_tracker import TokenTracker
from tests.conftest import FakeHttpClient, FakeLLM
from tests.unit.test_record_and_consolidate import M1, ctx, delta_first_window

EML = b"""From: agent@example.com
To: ops@example.com
Subject: RE: KING ONE - Delivery Assistance
Date: Tue, 3 Mar 2026 10:00:00 +0000
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

We can assist with husbandry services at Suez.
"""


def _blank_pdf(pages=1):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    return doc.tobytes()


# --- sniffing ---------------------------------------------------------------

def test_sniff_routes_eml_labelled_pdf_to_mail_parser():
    assert sniff_family("RE KING ONE - Delivery Assistance.pdf", "pdf", EML) == "eml"
    r = read_bytes("RE KING ONE - Delivery Assistance.pdf", "pdf", EML)
    assert r.method == "eml_parse"
    assert "husbandry services" in r.text and "Subject: RE: KING ONE" in r.text
    assert r.meta["sniffed_family"] == "eml" and r.note.startswith("declared pdf, bytes say eml")


def test_sniff_trusts_magic_bytes_over_names():
    assert sniff_family("x.pdf", "pdf", b"%PDF-1.4 ...") == "pdf"
    assert sniff_family("photo.pdf", "pdf", b"\x89PNG\r\n\x1a\n...") == "image"
    assert sniff_family("report.docx", "word", b"PK\x03\x04...") == "word"
    assert sniff_family("dump.bin", "telemetry", b"PK\x03\x04...") == "archive"
    assert sniff_family("notes.pdf", "pdf", b"just some text") == "other"


# --- identity documents -----------------------------------------------------

@pytest.mark.parametrize("name", ["AGABEY DEMIRCI PASSPORT.pdf", "seaman book.jpg", "Crew VISA copy.pdf", "COC_master.pdf"])
def test_identity_by_name(name):
    assert is_identity_document(name)


def test_identity_by_text_and_negative():
    assert is_identity_document("scan001.pdf", "REPUBLIC OF X\nPASSPORT\nP<TURDEMIRCI<<AGABEY<<<<<")
    assert not is_identity_document("Service Report 20260730.pdf", "Japan Engine Corporation service report T.R.H. 3076 h")


async def test_identity_document_is_classified_never_read_or_ocred(tmp_store):
    pdf = _blank_pdf()
    url = "https://example.invalid/att/X/MESUT%20AYLIKCI%20PASSPORT.pdf"
    http = FakeHttpClient({}, {url: (pdf, "application/pdf")})
    llm = FakeLLM([], ocr_reply="PASSPORT P<TUR...")
    ocr = PdfOcr(llm=llm, backend="vision")
    digester = AttachmentDigester(tmp_store, Fetcher(client=http, retries=1), llm, mode="digest", ocr=ocr)
    docs = await digester.process_mail_attachments([url], M1, "c" * 24, TokenTracker(stage="t"))
    d = docs[0]
    assert d["content_bearing"] is True and d["extraction_method"] == "skipped"
    assert d["kind_of_doc"] == "Identity document" and d["tags"] == ["identity_document"]
    assert d["extracted_text"] is None and d["audit"]["identity_document"] is True
    assert llm.chat_calls == [] and ocr.stats["calls"] == 0  # nothing sent to a model


# --- OCR ----------------------------------------------------------------------

async def test_scanned_pdf_is_ocred_via_vision_and_digested(tmp_store):
    pdf = _blank_pdf(pages=2)
    url = "https://example.invalid/att/X/Service%20Report%2020260730.pdf"
    http = FakeHttpClient({}, {url: (pdf, "application/pdf")})
    llm = FakeLLM([], ocr_reply="JAPAN ENGINE CORPORATION\nSERVICE REPORT\nIdle gear replaced. T.R.H. 3076 h")
    ocr = PdfOcr(llm=llm, backend="vision", max_pages=10)
    digester = AttachmentDigester(tmp_store, Fetcher(client=http, retries=1), llm, mode="digest", ocr=ocr)
    docs = await digester.process_mail_attachments([url], M1, "c" * 24, TokenTracker(stage="t"))
    d = docs[0]
    assert d["extraction_method"] == "pdf_ocr"
    assert "[page 1]" in d["extracted_text"] and "[page 2]" in d["extracted_text"] and "Idle gear replaced" in d["extracted_text"]
    assert d["audit"]["ocr_backend"] == "vision" and d["audit"]["ocr_pages"] == 2 and d["audit"]["reader_version"] == READER_VERSION
    assert ocr.stats["calls"] == 2 and digester.stats["ocr_pages"] == 2
    # 2 OCR calls (no response_format) + 1 digest call (json response_format)
    assert len(llm.chat_calls) == 3 and sum(1 for c in llm.chat_calls if c["response_format"]) == 1
    assert d["summary"] == "OMD sensor service note."  # FakeLLM's digest reply


async def test_ocr_page_cap_is_noted(tmp_store):
    pdf = _blank_pdf(pages=3)
    url = "https://example.invalid/att/X/long%20scan.pdf"
    http = FakeHttpClient({}, {url: (pdf, "application/pdf")})
    llm = FakeLLM([], ocr_reply="text")
    ocr = PdfOcr(llm=llm, backend="vision", max_pages=2)
    digester = AttachmentDigester(tmp_store, Fetcher(client=http, retries=1), llm, mode="digest", ocr=ocr)
    d = (await digester.process_mail_attachments([url], M1, "c" * 24, None))[0]
    assert d["audit"]["ocr_pages"] == 2 and "1 pages beyond PDF_OCR_MAX_PAGES=2" in d["audit"]["ocr_note"]


async def test_cached_listed_only_pdf_is_rebuilt_when_ocr_becomes_available(tmp_store):
    pdf = _blank_pdf()
    url = "https://example.invalid/att/X/SOF.pdf"
    http = FakeHttpClient({}, {url: (pdf, "application/pdf")})
    # First run: no OCR -> listed_only.
    d1 = (await AttachmentDigester(tmp_store, Fetcher(client=http, retries=1), FakeLLM([]), mode="digest").process_mail_attachments([url], M1, "c" * 24, None))[0]
    assert d1["extraction_method"] == "listed_only" and d1["audit"].get("ocr_backend") is None
    # Second run with OCR: the cached digest is upgraded, the file is not refetched.
    llm = FakeLLM([], ocr_reply="STATEMENT OF FACTS 20 July alloy debris found")
    dig = AttachmentDigester(tmp_store, Fetcher(client=http, retries=1), llm, mode="digest", ocr=PdfOcr(llm=llm, backend="vision"))
    d2 = (await dig.process_mail_attachments([url], M1, "c" * 24, None))[0]
    assert d2["extraction_method"] == "pdf_ocr" and "alloy debris" in d2["extracted_text"]
    assert dig.stats["fetched"] == 0 and dig.stats["cache_hits"] == 1
    assert len(d2["links"]) == 1  # link not duplicated


def test_ocr_backend_validation():
    with pytest.raises(ValueError):
        PdfOcr(llm=None, backend="vision")
    with pytest.raises(ValueError):
        PdfOcr(llm=None, backend="magic")
    assert PdfOcr(llm=None, backend="off").enabled is False


# --- translation ---------------------------------------------------------------

def test_needs_translation_by_script():
    assert needs_translation("紧固完螺栓，还是有漏烟")
    assert needs_translation("Насос заменён")
    assert not needs_translation("Vessel renewed the sensor")
    assert not needs_translation("Gemi İstanbul'a vardı — señor")  # accented Latin stays Latin
    assert not needs_translation(None) and not needs_translation("")


def test_split_normalise_strip():
    q = "紧固完螺栓，还是有漏烟 [EN: after tightening the bolts there was still smoke leaking]"
    assert split_quote(q) == ("紧固完螺栓，还是有漏烟", "after tightening the bolts there was still smoke leaking")
    assert strip_translation(q) == "紧固完螺栓，还是有漏烟"
    assert normalise_quote("判断水套密封圈老化所致。[English: judged to be due to aged seals]") == "判断水套密封圈老化所致。 [EN: judged to be due to aged seals]"
    assert normalise_quote("alarm still persists [EN: alarm still persists]") == "alarm still persists"  # English needs no bracket
    assert normalise_quote("plain english") == "plain english"
    assert split_quote("no bracket 原文") == ("no bracket 原文", None)


def test_apply_delta_normalises_quote_brackets(mini_casefile):
    r = new_record(mini_casefile)
    d = delta_first_window()
    d["add"]["faults"][0]["provenance"]["evidence_quote"] = "报警仍然存在 (English: alarm still present)"
    apply_delta(r, d, ctx(M1))
    assert r["faults"][0]["provenance"]["evidence_quote"] == "报警仍然存在 [EN: alarm still present]"


def test_validator_matches_on_original_not_translation(mini_casefile):
    r = new_record(mini_casefile)
    d = delta_first_window()
    d["add"]["faults"][0]["provenance"]["evidence_quote"] = "报警仍然存在 [EN: alarm still present]"
    apply_delta(r, d, ctx(M1))
    mails = {M1: "主机曲轴箱油雾探测器故障。报警仍然存在。We checked for terminal tightness & sensor cleaned , alarm still persists. Vessel renewed the sensor with spare one."}
    flags = validate_record(strip_sidecars(r), mails, {})
    assert not any(f.startswith("quote_not_found:f1") for f in flags)


async def test_translate_missing_adds_bracket_with_one_call(mini_casefile):
    r = new_record(mini_casefile)
    d = delta_first_window()
    d["add"]["faults"][0]["provenance"]["evidence_quote"] = "判断水套密封圈老化所致。"
    d["add"]["action_logs"][0]["outcome"]["evidence_quote"] = "紧固完螺栓，还是有漏烟"
    d["add"]["action_logs"][0]["provenance"]["evidence_quote"] = "紧固完螺栓，还是有漏烟"  # same original twice -> one entry
    apply_delta(r, d, ctx(M1))
    assert len(quotes_missing_translation(r)) == 3
    llm = FakeLLM([{"translations": ["judged to be caused by aged water-jacket sealing rings", "after tightening the bolts there was still smoke leaking"]}])
    n = await translate_missing(llm, "sys", r, TokenTracker(stage="translate_quotes"))
    assert n == 3
    assert r["faults"][0]["provenance"]["evidence_quote"] == "判断水套密封圈老化所致。 [EN: judged to be caused by aged water-jacket sealing rings]"
    assert r["action_logs"][0]["outcome"]["evidence_quote"].endswith("[EN: after tightening the bolts there was still smoke leaking]")
    sent = json.loads(llm.prompts[0][1].split("\n\n", 1)[1])
    assert sent["quotes"] == ["判断水套密封圈老化所致。", "紧固完螺栓，还是有漏烟"]  # deduplicated, ordered
    assert quotes_missing_translation(r) == []
    # Nothing left to translate -> no further call.
    assert await translate_missing(FakeLLM([]), "sys", r, None) == 0


async def test_translate_missing_tolerates_bad_reply(mini_casefile):
    r = new_record(mini_casefile)
    d = delta_first_window()
    d["add"]["faults"][0]["provenance"]["evidence_quote"] = "判断水套密封圈老化所致。"
    apply_delta(r, d, ctx(M1))
    assert await translate_missing(FakeLLM([{"translations": ["a", "b", "c"]}]), "sys", r, None) == 0
    assert r["faults"][0]["provenance"]["evidence_quote"] == "判断水套密封圈老化所致。"

