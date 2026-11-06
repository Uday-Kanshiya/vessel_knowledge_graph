"""Windowing arithmetic and mail HTML -> text, quote stripping, quote matching."""

import pytest

from src.extraction.mail_text import (
    html_to_text,
    mail_body_text,
    normalise_for_match,
    quote_found,
    strip_quoted_replies,
)
from src.extraction.windows import WindowSpec, window_count, windows, windows_after


def test_window_spec_rejects_bad_values():
    with pytest.raises(ValueError):
        WindowSpec(0, 0)
    with pytest.raises(ValueError):
        WindowSpec(5, 5)


def test_windows_never_exceed_size_and_overlap_one():
    spec = WindowSpec(5, 1)
    ws = windows(list(range(12)), spec)
    assert all(len(w) <= 5 for w in ws)
    assert ws == [[0, 1, 2, 3, 4], [4, 5, 6, 7, 8], [8, 9, 10, 11]]
    assert window_count(12, spec) == len(ws)


@pytest.mark.parametrize("n", [0, 1, 2, 5, 6, 7, 46, 63, 218])
def test_window_count_matches_windows(n):
    spec = WindowSpec(5, 1)
    assert window_count(n, spec) == len(windows(list(range(n)), spec))


def test_windows_no_overlap():
    ws = windows(list(range(7)), WindowSpec(5, 0))
    assert ws == [[0, 1, 2, 3, 4], [5, 6]]


def test_windows_after_resume_keeps_boundaries_stable():
    spec = WindowSpec(5, 1)
    pages = list(range(12))
    full = windows(pages, spec)
    # After the first window (last pagenum 4), the second and third remain.
    assert windows_after(pages, spec, 4, lambda p: p) == full[1:]
    assert windows_after(pages, spec, 11, lambda p: p) == []
    assert windows_after(pages, spec, -1, lambda p: p) == full


def test_html_to_text_keeps_header_and_body_lines(mail_html):
    text = html_to_text(mail_html["bbbbbbbbbbbbbbbbbbbbbbb1"])
    assert "From: Chief Engineer" in text
    assert "Vessel renewed the sensor with spare one" in text
    assert "&amp;" not in text and "terminal tightness & sensor cleaned" in text


def test_quote_stripping_cuts_original_message_but_not_header(mail_html):
    text = mail_body_text(mail_html["bbbbbbbbbbbbbbbbbbbbbbb2"], strip_quotes=True)
    assert "Please monitor the OMD for 48 hours" in text
    assert "Vessel renewed the sensor" not in text
    assert "[quoted earlier mail removed]" in text
    assert "From: Superintendent" in text  # the page's own header survives


def test_quote_stripping_can_be_disabled(mail_html):
    text = mail_body_text(mail_html["bbbbbbbbbbbbbbbbbbbbbbb2"], strip_quotes=False)
    assert "Vessel renewed the sensor" in text


def test_strip_on_wrote_marker():
    text = "From: a\nSubject: s\nDate: d\n\nreply text\n\nOn Mon, 1 Jan 2026 at 10:00, Bob <b@x> wrote:\n> old"
    out = strip_quoted_replies(text)
    assert "reply text" in out and "> old" not in out


def test_truncate_marks_cut():
    text = mail_body_text("<p>" + "x" * 100 + "</p>", strip_quotes=False, max_chars=50)
    assert text.startswith("x" * 50) and "[... cut at 50 chars]" in text


def test_normalise_and_quote_found():
    src = "Vessel  renewed the sensor – tried out and found “working satisfactory”."
    assert quote_found('renewed the sensor - tried out and found "working satisfactory"', src)
    assert quote_found("RENEWED THE SENSOR", src)
    assert not quote_found("replaced the sensor", src)
    assert not quote_found("", src)
    assert normalise_for_match("A–B  c") == "a-b c"
