"""
Shared fixtures. No network, no DB, no LLM in unit tests (03 §8).

    mini_export      the 3-mail fixture casefile (tests/fixtures/casefile_mini.json)
    mini_casefile    its Casefile
    tmp_store        a RunStore in a temp dir
    mail_html        the fixture mails' HTML by mail_id
    FakeLLM          scripted json_completion; chat_completion returns the digest JSON
                     (json response_format) or ocr_reply text (no response_format)
    FakeFetcher      serves mail HTML and attachment bytes from dicts
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.corpus.export import CasefileExport
from src.extraction.store import RunStore

FIXTURES = Path(__file__).parent / "fixtures"

MAIL_HTML = {
    "bbbbbbbbbbbbbbbbbbbbbbb1": """<html><body>
<p><strong>From:</strong> Chief Engineer <a href="mailto:cheng@vessel.example">cheng@vessel.example</a></p>
<p><strong>Subject:</strong> Main engine OMD fault rectified</p>
<p><strong>Date:</strong> 2026-06-21 05:56:05</p>
<p><strong>To:</strong> super@office.example</p>
<hr>
<p>Dear</p>
<p>Kindly note, Main engine crankcase oil detector fail alarm for unit no:2 activated in AMS on 20/06/2026.</p>
<p>OMD control Panel displays photodiode fault. Following checks has been carried out</p>
<p>We checked for terminal tightness &amp; sensor cleaned , alarm still persists.</p>
<p>Vessel renewed the sensor with spare one, tried out and found to be working satisfactory.</p>
<p>Requisition no- KRES26S0190 raised for OMD</p>
<p>Best Regards,</p><p>CE</p>
</body></html>""",
    "bbbbbbbbbbbbbbbbbbbbbbb2": """<html><body>
<p><strong>From:</strong> Superintendent <a href="mailto:super@office.example">super@office.example</a></p>
<p><strong>Subject:</strong> RE: Main engine OMD fault rectified</p>
<p><strong>Date:</strong> 2026-06-21 07:00:00</p>
<hr>
<p>Noted with thanks. Please monitor the OMD for 48 hours and revert.</p>
<p>-----Original Message-----</p>
<p>From: Chief Engineer</p>
<p>Vessel renewed the sensor with spare one, tried out and found to be working satisfactory.</p>
</body></html>""",
    "bbbbbbbbbbbbbbbbbbbbbbb3": """<html><body>
<p><strong>From:</strong> Chief Engineer <a href="mailto:cheng@vessel.example">cheng@vessel.example</a></p>
<p><strong>Subject:</strong> RE: Main engine OMD fault rectified</p>
<p><strong>Date:</strong> 2026-06-22 08:00:00</p>
<hr>
<p>OMD monitored for 48 hrs, no further alarm observed. Unit no:2 sensor working normal.</p>
</body></html>""",
}

# A tiny PNG (1x1) -> junk by name and by size; a fake "PDF" that has no text layer.
PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0300050001"
    "0d0a2db40000000049454e44ae426082"
)
FAKE_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"

ATTACHMENTS = {
    "https://example.invalid/att/AAA/image001.png": (PNG_1x1, "image/png"),
    "https://example.invalid/att/AAA/OMD%20service%20note.pdf": (FAKE_PDF, "application/pdf"),
}


@pytest.fixture
def mini_export() -> CasefileExport:
    return CasefileExport(FIXTURES / "casefile_mini.json")


@pytest.fixture
def mini_casefile(mini_export):
    return mini_export.get("aaaaaaaaaaaaaaaaaaaaaaa1")


@pytest.fixture
def tmp_store(tmp_path) -> RunStore:
    return RunStore(tmp_path / "data")


@pytest.fixture
def mail_html() -> dict[str, str]:
    return dict(MAIL_HTML)


class FakeLLM:
    """
    Scripted stand-in for LLMClient. `responses` is a list consumed in order by
    json_completion(); chat_completion() (digest calls) returns `digest_reply`.
    Records every prompt for assertions.
    """

    def __init__(self, responses: list, digest_reply: dict | None = None, model: str = "fake-model", ocr_reply: str = "transcribed page text"):
        self.responses = list(responses)
        self.ocr_reply = ocr_reply
        self.digest_reply = digest_reply or {
            "kind_of_doc": "Service note",
            "summary": "OMD sensor service note.",
            "tags": ["service_report"],
            "maker": "NA",
            "model": "NA",
            "equipment_details": "NA",
        }
        self.prompts: list[tuple[str | None, str]] = []
        self.chat_calls: list[dict] = []
        self.llm_config = {"model": model}

    async def json_completion(self, prompt, system_message=None, token_tracker=None, **kwargs):
        self.prompts.append((system_message, prompt))
        if not self.responses:
            raise AssertionError("FakeLLM ran out of scripted responses")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        if token_tracker is not None:
            token_tracker.add_call(100, 20, model="fake-model")
        return json.loads(json.dumps(r))

    async def chat_completion(self, messages, system_message=None, response_format=None, token_tracker=None, **kwargs):
        self.chat_calls.append({"messages": messages, "system": system_message, "response_format": response_format})
        if token_tracker is not None:
            token_tracker.add_call(50, 10, model="fake-model")
        # No response_format => a plain-text call (OCR transcription); JSON => digest.
        content = json.dumps(self.digest_reply) if response_format else self.ocr_reply
        return {"content": content, "finish_reason": "stop", "usage": None, "model": "fake-model", "id": "x"}


class FakeResponse:
    def __init__(self, data: bytes, content_type: str, status: int = 200):
        self.content = data
        self.status_code = status
        self.headers = {"content-type": content_type}


class FakeHttpClient:
    """Drop-in for httpx.AsyncClient.get over two dicts."""

    def __init__(self, mail_html: dict[str, str], attachments: dict[str, tuple[bytes, str]]):
        self.mail_html = mail_html
        self.attachments = attachments
        self.calls: list[str] = []

    async def get(self, url: str):
        self.calls.append(url)
        for mid, html in self.mail_html.items():
            if url.endswith(f"{mid}.html"):
                return FakeResponse(html.encode("utf-8"), "text/html")
        if url in self.attachments:
            data, ct = self.attachments[url]
            return FakeResponse(data, ct)
        return FakeResponse(b"not found", "text/plain", 404)

    async def aclose(self):
        pass


@pytest.fixture
def fake_llm_factory():
    return FakeLLM


@pytest.fixture
def fake_http():
    return FakeHttpClient(dict(MAIL_HTML), dict(ATTACHMENTS))
