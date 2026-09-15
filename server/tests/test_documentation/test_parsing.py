"""Parsers: typed elements with offsets, and honest failure."""

import pytest

from sutr.documentation.parsing import (
    LIST,
    NARRATIVE,
    TITLE,
    ParserUnavailableError,
    detect_kind,
    parse_document,
    parser_capabilities,
)
from sutr.documentation.parsing.pdf_parser import available as pdf_available
from sutr.models.document import KIND_DOCX, KIND_HTML, KIND_MARKDOWN, KIND_PDF, KIND_TEXT


def test_markdown_headings_become_titles_with_levels():
    result = parse_document(KIND_MARKDOWN, b"# Top\n\nBody text.\n\n## Sub\n\nMore.\n")
    titles = [(e.text, e.level) for e in result.elements if e.element_type == TITLE]
    assert titles == [("Top", 1), ("Sub", 2)]
    assert any(e.element_type == NARRATIVE and "Body text" in e.text for e in result.elements)


def test_markdown_list_items_are_one_list_element():
    result = parse_document(KIND_MARKDOWN, b"- alpha\n- beta\n- gamma\n")
    lists = [e for e in result.elements if e.element_type == LIST]
    assert len(lists) == 1
    assert "alpha" in lists[0].text and "gamma" in lists[0].text


def test_offsets_locate_each_element_in_the_source_text():
    """The whole citation story depends on this.

    The contract is containment, not equality: offsets bound the *region* of
    the source an element came from, while `element.text` is that region
    normalized (a heading without its `#`, a paragraph without its trailing
    newline). A citation has to be able to point a reader at the original
    passage, which the region does and the normalized text alone does not.
    """
    result = parse_document(KIND_MARKDOWN, b"# Refunds\n\nRefunds are allowed.\n\n- one\n- two\n")
    assert result.elements
    previous_end = 0
    for element in result.elements:
        assert 0 <= element.start_offset < element.end_offset <= len(result.text)
        region = result.text[element.start_offset : element.end_offset]
        assert element.text in region
        # Elements are in document order and do not overlap.
        assert element.start_offset >= previous_end
        previous_end = element.end_offset


def test_html_strips_script_and_style():
    html = b"<html><head><style>p{color:red}</style></head><body><h1>Title</h1>"
    html += b"<p>Visible.</p><script>alert('no')</script></body></html>"
    result = parse_document(KIND_HTML, html)
    assert "Visible." in result.text
    assert "alert" not in result.text
    assert "color:red" not in result.text
    assert [e.text for e in result.elements if e.element_type == TITLE] == ["Title"]


def test_text_paragraphs_split_on_blank_lines():
    result = parse_document(KIND_TEXT, b"First para.\n\nSecond para.\n")
    narratives = [e.text for e in result.elements if e.element_type == NARRATIVE]
    assert narratives == ["First para.", "Second para."]


def test_kind_is_detected_from_content_over_filename():
    """A .txt full of %PDF is a PDF. Trusting the filename hands the text
    parser a wall of binary and extracts business rules from it."""
    assert detect_kind(filename="notes.txt", content=b"%PDF-1.4\n%\xe2\xe3") == KIND_PDF
    assert detect_kind(filename="page.html", content=b"<html><body>hi</body></html>") == KIND_HTML
    assert detect_kind(filename="readme.md", content=b"# Title") == KIND_MARKDOWN
    assert detect_kind(filename="notes.txt", content=b"plain") == KIND_TEXT


def test_docx_is_read_from_the_zip_without_a_third_party_library():
    docx = _minimal_docx(["Heading", "A paragraph."])
    result = parse_document(KIND_DOCX, docx)
    assert "A paragraph." in result.text


def test_an_unreadable_pdf_reports_a_parse_error_not_a_silent_empty_document():
    available, _ = pdf_available()
    if not available:
        with pytest.raises(ParserUnavailableError):
            parse_document(KIND_PDF, b"%PDF-1.4 broken")
    else:
        # A truncated PDF must not come back as a successful empty parse:
        # downstream, "no rules found" and "could not read the file" are very
        # different answers.
        from sutr.documentation.parsing import ParseError

        with pytest.raises(ParseError):
            parse_document(KIND_PDF, b"%PDF-1.4 broken")


def test_capabilities_report_what_this_deployment_can_actually_read():
    kinds = {entry["kind"]: entry for entry in parser_capabilities()}
    assert kinds[KIND_TEXT]["available"] is True
    assert kinds[KIND_MARKDOWN]["available"] is True
    # PDF and OCR depend on optional software; whatever the answer, an
    # unavailable capability must carry a reason a user can act on.
    for kind in (KIND_PDF, "ocr"):
        if not kinds[kind]["available"]:
            assert kinds[kind]["unavailable_reason"]


def _minimal_docx(paragraphs: list[str]) -> bytes:
    """The smallest zip the docx parser will accept."""
    import io
    import zipfile

    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()
