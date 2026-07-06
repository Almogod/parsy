"""
Parsy — pytest test suite
Covers:
    * Unit tests for the text normalizer (date normalisation, heading tree,
      table cleaning, metrics, markdown / HTML / JSON output)
    * Unit tests for the document router (extension routing, PDF inspection
      heuristics, edge cases)
    * Unit tests for the input validator (size limits, extension allow-list,
      magic bytes, zip-bomb detection, filename sanitisation)
    * Integration tests for the FastStructuralParser on plain-text payloads
    * Integration tests for the StructuredParser on CSV and JSON payloads

Run with:
    cd backend
    pytest tests/ -v --tb=short
"""
import sys, os

# Make backend modules importable regardless of where pytest is invoked from.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import io
import json
import zipfile

import pytest

# ── Module imports ─────────────────────────────────────────────────────────
from normalizer import (
    normalize,
    normalize_dates,
    normalize_table,
    validate_heading_tree,
    compute_metrics,
    blocks_to_markdown,
    blocks_to_json_schema,
)
from fast_parser import ParsedBlock, FastParseResult
from router import DocumentRouter, Route
from input_validator import InputValidator, ValidationError, ALLOWED_EXTENSIONS


# ═══════════════════════════════════════════════════════════════════════════════
# ── Fixtures ──────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def simple_blocks():
    """A minimal list of ParsedBlocks for normalizer tests."""
    return [
        ParsedBlock("heading",   1, "Introduction",          page=0),
        ParsedBlock("paragraph", 0, "Some body text here.",  page=0),
        ParsedBlock("heading",   3, "Sub-section",           page=0),  # should be corrected to H2
        ParsedBlock("list",      0, "First item",            page=0),
        ParsedBlock("table",     0, "",                      page=1,
                    table_data=[["Name", "Score"], ["Alice", "95"], ["Bob", "87"]]),
    ]


@pytest.fixture
def sample_result(simple_blocks):
    return FastParseResult(
        blocks=simple_blocks,
        page_count=2,
        raw_text="Introduction\nSome body text here.\nSub-section\nFirst item",
        tables=[[["Name", "Score"], ["Alice", "95"], ["Bob", "87"]]],
        metadata={"title": "Test Doc", "pageCount": 2},
    )


@pytest.fixture
def validator():
    return InputValidator(max_size_bytes=10 * 1024 * 1024)  # 10 MB cap for tests


# ═══════════════════════════════════════════════════════════════════════════════
# ── Normalizer tests ──────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormalizer:

    def test_date_normalization_dmy(self):
        assert normalize_dates("Date: 15/06/2024") == "Date: 2024-06-15"

    def test_date_normalization_ymd(self):
        assert normalize_dates("Created 2024-01-31") == "Created 2024-01-31"

    def test_date_normalization_text(self):
        result = normalize_dates("Published June 15, 2024")
        assert "2024-06-15" in result

    def test_date_normalization_no_change(self):
        text = "No dates in this text."
        assert normalize_dates(text) == text

    def test_date_normalization_invalid_month(self):
        # Month 13 should not crash — original text returned
        result = normalize_dates("Date: 99/13/2024")
        assert "99/13/2024" in result or "99" in result  # unchanged or partially changed

    def test_heading_tree_jump_corrected(self):
        """H1 → H3 jump should be corrected to H1 → H2."""
        blocks = [
            ParsedBlock("heading", 1, "Title",    page=0),
            ParsedBlock("heading", 3, "Too deep", page=0),
        ]
        corrected = validate_heading_tree(blocks)
        heading_levels = [b.level for b in corrected if b.block_type == "heading"]
        assert heading_levels == [1, 2], f"Expected [1, 2], got {heading_levels}"

    def test_heading_tree_no_jump(self):
        blocks = [
            ParsedBlock("heading", 1, "A", page=0),
            ParsedBlock("heading", 2, "B", page=0),
            ParsedBlock("heading", 3, "C", page=0),
        ]
        corrected = validate_heading_tree(blocks)
        assert [b.level for b in corrected if b.block_type == "heading"] == [1, 2, 3]

    def test_normalize_table_strips_whitespace(self):
        rows = [["  Name  ", "  Score  "], ["  Alice  ", "  95  "]]
        cleaned = normalize_table(rows)
        assert cleaned[0] == ["Name", "Score"]
        assert cleaned[1] == ["Alice", "95"]

    def test_normalize_table_pads_rows(self):
        rows = [["A", "B", "C"], ["X"]]
        cleaned = normalize_table(rows)
        assert len(cleaned[1]) == 3
        assert cleaned[1][1] == ""

    def test_normalize_table_removes_empty_rows(self):
        rows = [["A", "B"], ["", ""], ["C", "D"]]
        cleaned = normalize_table(rows)
        assert len(cleaned) == 2

    def test_normalize_table_empty_input(self):
        assert normalize_table([]) == []

    def test_compute_metrics_basic(self, sample_result):
        metrics = compute_metrics(
            sample_result.raw_text, sample_result.blocks,
            sample_result.tables, sample_result.metadata
        )
        assert metrics["wordCount"] > 0
        assert metrics["charCount"] > 0
        assert "readingTime" in metrics
        assert "parsedAt" in metrics
        assert "tableCount" in metrics

    def test_compute_metrics_reading_time(self):
        # 200 words should = 1 min reading time
        text = " ".join(["word"] * 200)
        metrics = compute_metrics(text, [], [], {})
        assert metrics["readingTime"] == "1 min"

    def test_blocks_to_markdown_headings(self):
        blocks = [
            ParsedBlock("heading",   1, "Title",   page=0),
            ParsedBlock("paragraph", 0, "Content", page=0),
        ]
        md = blocks_to_markdown(blocks)
        assert "# Title" in md
        assert "Content" in md

    def test_blocks_to_markdown_table(self):
        blocks = [
            ParsedBlock("table", 0, "", page=0,
                        table_data=[["H1", "H2"], ["R1", "R2"]]),
        ]
        md = blocks_to_markdown(blocks)
        assert "|" in md
        assert "H1" in md

    def test_blocks_to_markdown_list(self):
        blocks = [ParsedBlock("list", 0, "Item one", page=0)]
        md = blocks_to_markdown(blocks)
        assert "- Item one" in md

    def test_blocks_to_json_schema_structure(self):
        blocks = [
            ParsedBlock("heading",   1, "Section", page=0),
            ParsedBlock("paragraph", 0, "Text",    page=0),
        ]
        schema = blocks_to_json_schema(blocks, {"title": "Doc"}, [])
        assert "sections" in schema
        assert "metadata" in schema
        assert len(schema["sections"]) == 1
        assert schema["sections"][0]["heading"] == "Section"

    def test_normalize_full_pipeline(self, sample_result):
        """End-to-end: normalize returns valid NormalizedOutput for all formats."""
        for fmt in ("markdown", "plaintext", "json", "html", "csv"):
            out = normalize(sample_result, output_format=fmt)
            assert out.markdown
            assert out.plaintext
            assert out.html
            assert isinstance(out.json_data, dict)
            assert isinstance(out.metrics, dict)

    def test_normalize_markdown_no_raw_md_symbols_in_plain(self, sample_result):
        out = normalize(sample_result, output_format="plaintext")
        # Plaintext must strip heading markers
        assert "##" not in out.plaintext

    def test_normalize_csv_tables_present(self, sample_result):
        out = normalize(sample_result, output_format="csv")
        assert len(out.csv_tables) > 0
        # Should contain header row
        assert "Name" in out.csv_tables[0]


# ═══════════════════════════════════════════════════════════════════════════════
# ── Document Router tests ─────────────────────────────────────════════════════
# ═══════════════════════════════════════════════════════════════════════════════

class TestDocumentRouter:

    @pytest.fixture(autouse=True)
    def router(self):
        self.router = DocumentRouter()

    def test_csv_routes_to_structured(self):
        d = self.router.route("data.csv", b"col1,col2\nval1,val2")
        assert d.route == Route.STRUCTURED
        assert d.confidence >= 0.9

    def test_json_routes_to_structured(self):
        d = self.router.route("data.json", b'{"key": "value"}')
        assert d.route == Route.STRUCTURED

    def test_xlsx_routes_to_structured(self):
        d = self.router.route("report.xlsx", b"PK\x03\x04dummy_content")
        assert d.route == Route.STRUCTURED

    def test_xml_routes_to_structured(self):
        d = self.router.route("config.xml", b"<root><item>test</item></root>")
        assert d.route == Route.STRUCTURED

    def test_txt_routes_to_fast_text(self):
        d = self.router.route("notes.txt", b"Hello world, plain text document.")
        assert d.route == Route.FAST_TEXT

    def test_html_routes_to_fast_text(self):
        d = self.router.route("page.html", b"<html><body><p>Hello</p></body></html>")
        assert d.route == Route.FAST_TEXT

    def test_md_routes_to_markdown(self):
        d = self.router.route("readme.md", b"# Title\nContent here.")
        assert d.route == Route.MARKDOWN
        assert d.confidence >= 0.9

    def test_docx_routes_to_fast_text(self):
        # We can't create a real DOCX in tests, but we can verify the fallback path
        d = self.router.route("doc.docx", b"PK\x03\x04invalid_zip_content")
        # Should fall back to FAST_TEXT (the inspect_docx catches exceptions)
        assert d.route in (Route.FAST_TEXT, Route.VISION_OCR)

    def test_unknown_extension_fallback(self):
        d = self.router.route("file.weird", b"some bytes")
        assert d.route == Route.FAST_TEXT
        assert d.confidence == 0.5

    def test_rtf_routes_to_fast_text(self):
        d = self.router.route("document.rtf", b"{\\rtf1 content}")
        assert d.route == Route.FAST_TEXT

    def test_decision_has_required_fields(self):
        d = self.router.route("file.txt", b"content")
        assert hasattr(d, "route")
        assert hasattr(d, "confidence")
        assert hasattr(d, "reasons")
        assert hasattr(d, "page_count")
        assert hasattr(d, "estimated_complexity")
        assert hasattr(d, "recommended_workers")
        assert 0.0 <= d.confidence <= 1.0

    def test_structured_route_has_low_complexity(self):
        d = self.router.route("data.csv", b"a,b\n1,2")
        assert d.estimated_complexity == "low"

    def test_pdf_corrupt_routes_to_ocr(self):
        d = self.router.route("file.pdf", b"not a real pdf at all")
        assert d.route == Route.VISION_OCR
        assert d.confidence >= 0.8


# ═══════════════════════════════════════════════════════════════════════════════
# ── Input Validator tests ─────────────────────────────────────────════════════
# ═══════════════════════════════════════════════════════════════════════════════

class TestInputValidator:

    @pytest.fixture(autouse=True)
    def setup(self):
        self.v = InputValidator(max_size_bytes=5 * 1024 * 1024)  # 5 MB

    def test_valid_text_file(self):
        result = self.v.validate("document.txt", b"Hello world!")
        assert result.filename == "document.txt"
        assert result.extension == "txt"

    def test_empty_file_rejected(self):
        with pytest.raises(ValidationError, match="empty"):
            self.v.validate("empty.txt", b"")

    def test_oversized_file_rejected(self):
        big_data = b"x" * (6 * 1024 * 1024)  # 6 MB > 5 MB cap
        with pytest.raises(ValidationError, match="limit"):
            self.v.validate("big.txt", big_data)

    def test_disallowed_extension_rejected(self):
        with pytest.raises(ValidationError, match="not permitted"):
            self.v.validate("script.exe", b"MZ executable")

    def test_disallowed_extension_php(self):
        with pytest.raises(ValidationError, match="not permitted"):
            self.v.validate("shell.php", b"<?php echo shell_exec($_GET['cmd']); ?>")

    def test_path_traversal_stripped(self):
        result = self.v.validate("../../etc/passwd.txt", b"root:x:0:0")
        assert ".." not in result.filename
        assert "/" not in result.filename

    def test_null_byte_in_filename_rejected(self):
        with pytest.raises(ValidationError, match="null bytes"):
            self.v.validate("file\x00.txt", b"content")

    def test_png_magic_bytes_valid(self):
        png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        result = self.v.validate("image.png", png_header)
        assert result.detected_format == "png"

    def test_png_magic_bytes_mismatch_raises(self):
        """A .png file with wrong magic bytes should be rejected in strict mode."""
        fake_png = b"This is not a PNG file at all\x00\x00"
        with pytest.raises(ValidationError, match="magic bytes"):
            self.v.validate("image.png", fake_png)

    def test_non_strict_magic_mismatch_warns(self):
        """In non-strict mode, magic mismatch produces a warning, not an error."""
        v = InputValidator(max_size_bytes=1024 * 1024, strict_magic=False)
        fake_png = b"This is not a PNG file at all\x00\x00"
        result = v.validate("image.png", fake_png)
        assert any("magic" in w.lower() for w in result.warnings)

    def test_all_allowed_extensions_in_set(self):
        """Every extension in ALLOWED_EXTENSIONS should pass the extension check."""
        for ext in ALLOWED_EXTENSIONS:
            result = self.v.validate(f"test.{ext}", b"placeholder content for {ext}")
            assert result.extension == ext

    def test_zip_bomb_detection(self):
        """Construct a genuine but safe zip that triggers the ratio heuristic."""
        # Build a zip with claimed huge size but small actual content
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # Write a lot of repetitive content (compresses very well)
            zf.writestr("content.xml", "A" * 100_000)
        compressed = buf.getvalue()

        # The default cap is 200×; our 100 KB compresses to ~100 bytes → ratio ~1000×
        v = InputValidator(max_size_bytes=10 * 1024 * 1024)
        with pytest.raises(ValidationError, match="ratio|zip.?bomb|compression"):
            v.validate("bomb.xlsx", compressed)

    def test_pdf_encryption_warning(self):
        """PDFs with /Encrypt dictionary get a warning, not an error."""
        pdf_bytes = b"%PDF-1.4\n/Encrypt /Standard" + b"\x00" * 100
        result = self.v.validate("secure.pdf", pdf_bytes)
        assert any("password" in w.lower() or "encrypt" in w.lower()
                   for w in result.warnings)


# ═══════════════════════════════════════════════════════════════════════════════
# ── FastStructuralParser integration tests (text formats only) ────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class TestFastParserTextFormats:

    @pytest.fixture(autouse=True)
    def parser(self):
        from fast_parser import FastStructuralParser
        self.parser = FastStructuralParser(max_workers=1)

    @pytest.mark.asyncio
    async def test_plain_text_parse(self):
        data = b"INTRODUCTION\nThis is paragraph text.\n- A list item"
        result = await self.parser.parse("test.txt", data)
        assert result.page_count >= 1
        block_types = {b.block_type for b in result.blocks}
        assert "heading" in block_types
        assert "paragraph" in block_types

    @pytest.mark.asyncio
    async def test_markdown_parse(self):
        data = b"# Heading\n\n## Sub-heading\n\nParagraph text here.\n\n- List item 1\n- List item 2"
        result = await self.parser.parse("doc.md", data)
        headings = [b for b in result.blocks if b.block_type == "heading"]
        assert len(headings) >= 2
        assert headings[0].level == 1
        assert headings[1].level == 2

    @pytest.mark.asyncio
    async def test_markdown_table_extraction(self):
        data = b"# Title\n\n| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
        result = await self.parser.parse("table.md", data)
        table_blocks = [b for b in result.blocks if b.block_type == "table"]
        assert len(table_blocks) >= 1
        assert len(result.tables) >= 1

    @pytest.mark.asyncio
    async def test_html_parse(self):
        data = b"<html><body><h1>Title</h1><p>Paragraph.</p></body></html>"
        result = await self.parser.parse("page.html", data)
        headings = [b for b in result.blocks if b.block_type == "heading"]
        assert len(headings) >= 1

    @pytest.mark.asyncio
    async def test_html_strips_script_tags(self):
        data = b"<html><head><script>alert('xss')</script></head><body><p>Safe</p></body></html>"
        result = await self.parser.parse("safe.html", data)
        raw_combined = " ".join(b.content for b in result.blocks)
        assert "alert" not in raw_combined

    @pytest.mark.asyncio
    async def test_unknown_extension_falls_back_to_text(self):
        data = b"Some text content in an unusual file."
        result = await self.parser.parse("file.weird", data)
        assert result.page_count >= 1

    @pytest.mark.asyncio
    async def test_empty_text_returns_result(self):
        result = await self.parser.parse("empty.txt", b"   \n  \n  ")
        # Should return a valid FastParseResult with no blocks
        assert isinstance(result.blocks, list)


# ═══════════════════════════════════════════════════════════════════════════════
# ── StructuredParser integration tests ───────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class TestStructuredParser:

    @pytest.fixture(autouse=True)
    def parser(self):
        from structured_parser import StructuredParser
        self.parser = StructuredParser()

    @pytest.mark.asyncio
    async def test_csv_parse(self):
        data = b"Name,Score,Grade\nAlice,95,A\nBob,82,B\nCarol,71,C"
        result = await self.parser.parse("grades.csv", data)
        assert result.page_count >= 1
        assert len(result.tables) >= 1
        assert result.metadata.get("rows") == 3
        assert result.metadata.get("columns") == 3

    @pytest.mark.asyncio
    async def test_csv_schema_detection(self):
        data = b"id,value,date\n1,3.14,2024-01-01\n2,2.72,2024-06-15"
        result = await self.parser.parse("data.csv", data)
        schema = result.metadata.get("schema", "")
        assert "id" in schema
        assert "value" in schema

    @pytest.mark.asyncio
    async def test_csv_empty_returns_result(self):
        result = await self.parser.parse("empty.csv", b"")
        assert isinstance(result.blocks, list)

    @pytest.mark.asyncio
    async def test_json_parse_object(self):
        data = json.dumps({"name": "Parsy", "version": "3.0", "active": True}).encode()
        result = await self.parser.parse("config.json", data)
        assert result.metadata.get("rootType") == "dict"

    @pytest.mark.asyncio
    async def test_json_parse_array(self):
        records = [{"id": i, "value": i * 2} for i in range(5)]
        data = json.dumps(records).encode()
        result = await self.parser.parse("records.json", data)
        assert result.metadata.get("rootType") == "list"
        # Should produce a table from the list of dicts
        assert len(result.tables) >= 1

    @pytest.mark.asyncio
    async def test_json_parse_invalid(self):
        data = b"{ this is not valid JSON }"
        result = await self.parser.parse("bad.json", data)
        # Must return a result with an error block rather than crashing
        error_blocks = [b for b in result.blocks if "Error" in b.content or "error" in b.content.lower()]
        assert len(error_blocks) >= 1

    @pytest.mark.asyncio
    async def test_xml_parse(self):
        data = b"""<?xml version="1.0"?>
        <catalog>
            <book id="1"><title>Document Intelligence</title></book>
            <book id="2"><title>OCR Mastery</title></book>
        </catalog>"""
        result = await self.parser.parse("catalog.xml", data)
        assert result.page_count >= 1
        combined = " ".join(b.content for b in result.blocks)
        assert "Document Intelligence" in combined or "catalog" in combined.lower()
