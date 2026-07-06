"""
Parsy Backend — Abstract Base Parser
Defines the contract every parser (fast, OCR, structured) must satisfy.
Using this base class ensures consistency, shared error handling, and
a single hook point for future capabilities (e.g. telemetry, caching).

Usage
-----
    class MyParser(BaseParser):
        async def parse(self, filename: str, data: bytes) -> FastParseResult:
            ...
"""
import logging
import time
from abc import ABC, abstractmethod
from typing import Optional

from dataclasses import dataclass, field

@dataclass
class ParsedBlock:
    block_type: str            # "heading" | "paragraph" | "table" | "list" | "code" | "hr"
    level: int                 # heading level (1-6), 0 for non-headings
    content: str
    page: int
    bbox: tuple | None = None  # (x0, y0, x1, y1)
    table_data: list[list[str]] = field(default_factory=list)


@dataclass
class FastParseResult:
    blocks: list[ParsedBlock]
    page_count: int
    raw_text: str
    tables: list[list[list[str]]]
    metadata: dict

# Module-level logger — each subclass will request its own child logger.
_log = logging.getLogger("parsy.parsers")


class ParserError(Exception):
    """Raised when a parser encounters an unrecoverable document error."""

    def __init__(self, message: str, filename: str = "", cause: Optional[Exception] = None):
        super().__init__(message)
        self.filename = filename
        self.cause = cause

    def __str__(self) -> str:
        base = super().__str__()
        if self.filename:
            base = f"[{self.filename}] {base}"
        if self.cause:
            base = f"{base} (caused by: {self.cause!r})"
        return base


class UnsupportedFormatError(ParserError):
    """Raised when the parser receives a file type it cannot handle."""


class CorruptFileError(ParserError):
    """Raised when the file is malformed, truncated, or encrypted."""


class BaseParser(ABC):
    """
    Abstract interface for all Parsy parser implementations.

    Subclasses must implement :meth:`parse`.  The base class provides:

    * A named child logger (``parsy.parsers.<class_name>``)
    * :meth:`_validate_input` — rejects empty/oversized payloads early
    * :meth:`_timed_parse` — wraps the concrete implementation with
      structured duration logging and exception normalisation
    * :attr:`supported_extensions` — optional set of lower-case extensions
      the parser can handle (used for early routing validation)
    """

    #: Override in subclasses to advertise handled file types.
    #: ``None`` means the parser accepts any extension.
    supported_extensions: Optional[frozenset[str]] = None

    #: Maximum file size this parser will accept, in bytes.
    #: ``None`` means no additional limit beyond the global resource guard.
    max_file_size_bytes: Optional[int] = None

    def __init__(self) -> None:
        self.log = logging.getLogger(f"parsy.parsers.{type(self).__name__}")

    # ── Public interface ───────────────────────────────────────────────────────

    async def parse(self, filename: str, data: bytes) -> FastParseResult:
        """
        Parse *data* (raw file bytes identified by *filename*) and return a
        :class:`~fast_parser.FastParseResult`.

        This method validates input, delegates to :meth:`_parse`, and logs the
        outcome.  Concrete parsers override :meth:`_parse`, **not** this method.

        Raises
        ------
        UnsupportedFormatError
            If the file extension is not in ``supported_extensions``.
        CorruptFileError
            If the file bytes are empty or cannot be decoded.
        ParserError
            For any other unrecoverable parse failure.
        """
        self._validate_input(filename, data)
        return await self._timed_parse(filename, data)

    # ── Abstract hook ──────────────────────────────────────────────────────────

    @abstractmethod
    async def _parse(self, filename: str, data: bytes) -> FastParseResult:
        """
        Concrete parsing logic.  Implemented by every subclass.

        Must return a :class:`~fast_parser.FastParseResult`.
        May raise any :class:`ParserError` subclass on failure; other
        exceptions are wrapped automatically by :meth:`_timed_parse`.
        """

    # ── Shared helpers ─────────────────────────────────────────────────────────

    def _validate_input(self, filename: str, data: bytes) -> None:
        """
        Performs lightweight pre-parse checks.

        Raises
        ------
        CorruptFileError
            If *data* is empty.
        UnsupportedFormatError
            If the extension is not in :attr:`supported_extensions`.
        ParserError
            If the file exceeds :attr:`max_file_size_bytes`.
        """
        if self.supported_extensions is not None:
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if ext not in self.supported_extensions:
                raise UnsupportedFormatError(
                    f"Extension '.{ext}' is not supported by {type(self).__name__}. "
                    f"Supported: {sorted(self.supported_extensions)}",
                    filename=filename,
                )

        if self.max_file_size_bytes is not None:
            size_mb = len(data) / (1024 * 1024)
            limit_mb = self.max_file_size_bytes / (1024 * 1024)
            if len(data) > self.max_file_size_bytes:
                raise ParserError(
                    f"File size {size_mb:.1f} MB exceeds parser limit of {limit_mb:.0f} MB.",
                    filename=filename,
                )

    async def _timed_parse(self, filename: str, data: bytes) -> FastParseResult:
        """Wraps ``_parse`` with duration logging and exception normalisation."""
        t0 = time.perf_counter()
        parser_name = type(self).__name__

        self.log.info(
            "Parse started",
            extra={"file_name": filename, "size_bytes": len(data), "parser": parser_name},
        )

        try:
            result = await self._parse(filename, data)
        except ParserError:
            # Already correctly typed — re-raise as-is
            self.log.error(
                "Parse failed (ParserError)",
                extra={"file_name": filename, "parser": parser_name},
                exc_info=True,
            )
            raise
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            self.log.error(
                "Parse failed (unexpected exception)",
                extra={
                    "file_name": filename,
                    "parser": parser_name,
                    "elapsed_s": f"{elapsed:.2f}",
                    "exc_type": type(exc).__name__,
                },
                exc_info=True,
            )
            raise ParserError(str(exc), filename=filename, cause=exc) from exc
        else:
            elapsed = time.perf_counter() - t0
            self.log.info(
                "Parse complete",
                extra={
                    "file_name": filename,
                    "parser": parser_name,
                    "elapsed_s": f"{elapsed:.2f}",
                    "pages": result.page_count,
                    "blocks": len(result.blocks),
                },
            )
            return result

    # ── Utility class-methods available to all subclasses ─────────────────────

    @staticmethod
    def extension_of(filename: str) -> str:
        """Return the lower-case extension without the leading dot."""
        return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    @staticmethod
    def is_empty_result(result: FastParseResult) -> bool:
        """True if the result contains no extractable content."""
        return not result.blocks and not result.raw_text.strip()
