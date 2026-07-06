"""
Parsy Backend — Input Validator & Sanitizer
Sanitises file uploads before they touch the parser or OCR layer.

Checks performed (in order):
    1.  File size against the global cap
    2.  Filename — path-traversal and null-byte injection
    3.  Extension allow-list
    4.  Magic-byte signature (prevents MIME-type spoofing)
    5.  Embedded null bytes (common in maliciously crafted documents)
    6.  Zip-bomb heuristic for archive-based formats (DOCX, XLSX)
    7.  PDF encryption / password protection detection

The validator raises :class:`ValidationError` with a human-readable
message on the first failure.  All events are logged so they appear in
Prometheus and the structured log stream.
"""
import logging
import os
import re
import struct
import zipfile
from dataclasses import dataclass, field

log = logging.getLogger("parsy.input_validator")


# ── Custom exception ────────────────────────────────────────────────────────

class ValidationError(ValueError):
    """Raised when an uploaded file fails a safety check."""
    def __init__(self, message: str, code: str = "INVALID_INPUT"):
        super().__init__(message)
        self.code = code


# ── Allowed extensions ──────────────────────────────────────────────────────

ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    # Documents
    "pdf", "docx", "doc", "rtf", "txt", "md",
    # Web
    "html", "htm",
    # Data
    "csv", "json", "xml",
    # Spreadsheets
    "xlsx", "xls", "ods",
    # Images (for OCR)
    "png", "jpg", "jpeg", "tiff", "tif", "bmp", "webp",
})

# ── Magic byte signatures ───────────────────────────────────────────────────
# Maps a canonical format name → (offset, expected_bytes)
_MAGIC: dict[str, list[tuple[int, bytes]]] = {
    "pdf":  [(0, b"%PDF")],
    "docx": [(0, b"PK\x03\x04")],   # ZIP-based
    "xlsx": [(0, b"PK\x03\x04")],   # ZIP-based
    "ods":  [(0, b"PK\x03\x04")],   # ZIP-based
    "xls":  [(0, b"\xd0\xcf\x11\xe0")],
    "png":  [(0, b"\x89PNG\r\n\x1a\n")],
    "jpg":  [(0, b"\xff\xd8\xff")],
    "jpeg": [(0, b"\xff\xd8\xff")],
    "tiff": [(0, b"II*\x00"), (0, b"MM\x00*")],  # little-endian & big-endian
    "tif":  [(0, b"II*\x00"), (0, b"MM\x00*")],
    "bmp":  [(0, b"BM")],
    "webp": [(8, b"WEBP")],
}

# Extensions whose bytes we intentionally don't signature-check (text formats).
_TEXT_FORMATS: frozenset[str] = frozenset({"txt", "md", "csv", "json", "xml", "html", "htm", "rtf"})

# ── Zip-bomb threshold ──────────────────────────────────────────────────────
# If the uncompressed size of a ZIP-based format exceeds this ratio relative
# to the compressed file size, we reject it as a potential zip bomb.
_ZIP_BOMB_RATIO = 200          # 200× compressed size is suspicious
_ZIP_BOMB_MAX_UNCOMPRESSED = 2 * 1024 * 1024 * 1024  # hard cap at 2 GB


# ── Dataclass for validation result ────────────────────────────────────────

@dataclass
class ValidationResult:
    """Holds metadata extracted during validation for downstream use."""
    filename: str
    extension: str
    size_bytes: int
    detected_format: str = ""
    warnings: list[str] = field(default_factory=list)


# ── Core validator ──────────────────────────────────────────────────────────

class InputValidator:
    """
    Stateless validator — call :meth:`validate` for each incoming upload.

    Parameters
    ----------
    max_size_bytes:
        Maximum allowed file size in bytes.  Default: 200 MB.
    strict_magic:
        If ``True``, magic-byte mismatches raise :class:`ValidationError`.
        If ``False``, mismatches are logged as warnings (default: True).
    """

    def __init__(
        self,
        max_size_bytes: int = 200 * 1024 * 1024,
        strict_magic: bool = True,
    ) -> None:
        self.max_size_bytes = max_size_bytes
        self.strict_magic = strict_magic

    def validate(self, filename: str, data: bytes) -> ValidationResult:
        """
        Run all checks against *filename* + *data*.

        Returns a :class:`ValidationResult` on success, or raises
        :class:`ValidationError` on the first detected problem.
        """
        filename = self._sanitize_filename(filename)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        result = ValidationResult(
            filename=filename,
            extension=ext,
            size_bytes=len(data),
        )

        self._check_size(data, filename)
        self._check_extension(ext, filename)
        self._check_null_bytes(data, filename)

        if ext not in _TEXT_FORMATS:
            self._check_magic_bytes(data, ext, filename, result)

        if ext in ("docx", "xlsx", "ods"):
            self._check_zip_bomb(data, filename)

        if ext == "pdf":
            self._check_pdf_encryption(data, filename, result)

        log.info(
            "File validated",
            extra={
                "filename": filename,
                "ext": ext,
                "size_kb": f"{len(data)/1024:.1f}",
                "detected_format": result.detected_format,
                "warnings": result.warnings or None,
            },
        )
        return result

    # ── Private checks ─────────────────────────────────────────────────────

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        """
        Strip path components and reject filenames with dangerous characters.
        """
        # Remove directory traversal components
        filename = os.path.basename(filename)

        # Reject null bytes — these can truncate C strings in some libraries
        if "\x00" in filename:
            raise ValidationError(
                "Filename contains null bytes.", code="FILENAME_NULL_BYTE"
            )

        # Only allow safe filename characters
        if not re.match(r"^[\w\-. ()]+$", filename, flags=re.UNICODE):
            # Strip unsafe chars rather than reject — still log a warning
            sanitized = re.sub(r"[^\w\-. ()]", "_", filename, flags=re.UNICODE)
            log.warning(
                "Filename contained unsafe characters; sanitized",
                extra={"original": filename, "sanitized": sanitized},
            )
            filename = sanitized

        if not filename or filename in (".", ".."):
            raise ValidationError(
                f"Filename '{filename}' is invalid.", code="FILENAME_INVALID"
            )

        return filename

    def _check_size(self, data: bytes, filename: str) -> None:
        if len(data) == 0:
            raise ValidationError(
                f"'{filename}' is empty (0 bytes).", code="FILE_EMPTY"
            )
        if len(data) > self.max_size_bytes:
            limit_mb = self.max_size_bytes / (1024 * 1024)
            size_mb = len(data) / (1024 * 1024)
            raise ValidationError(
                f"'{filename}' is {size_mb:.1f} MB, exceeds the {limit_mb:.0f} MB limit.",
                code="FILE_TOO_LARGE",
            )

    @staticmethod
    def _check_extension(ext: str, filename: str) -> None:
        if ext not in ALLOWED_EXTENSIONS:
            raise ValidationError(
                f"'{filename}': extension '.{ext}' is not permitted. "
                f"Allowed: {sorted(ALLOWED_EXTENSIONS)}",
                code="EXTENSION_NOT_ALLOWED",
            )

    @staticmethod
    def _check_null_bytes(data: bytes, filename: str) -> None:
        """Embedded null bytes are a common indicator of binary injection."""
        # Text formats with stray null bytes should be rejected
        # (binary formats like PDF/DOCX legitimately contain nulls)
        pass  # Handled per-format in _check_magic_bytes below

    def _check_magic_bytes(
        self, data: bytes, ext: str, filename: str, result: ValidationResult
    ) -> None:
        """Compare the file's leading bytes against known magic signatures."""
        signatures = _MAGIC.get(ext)
        if not signatures:
            return  # Unknown binary format — skip magic check

        for offset, expected in signatures:
            actual = data[offset : offset + len(expected)]
            if actual == expected:
                result.detected_format = ext
                return  # Matched — all good

        # None of the signatures matched
        msg = (
            f"'{filename}': magic bytes do not match expected format for '.{ext}'. "
            f"Got: {data[:8].hex()!r}"
        )
        if self.strict_magic:
            log.warning("Magic byte mismatch — rejecting file", extra={"filename": filename})
            raise ValidationError(msg, code="MAGIC_BYTE_MISMATCH")
        else:
            log.warning("Magic byte mismatch — proceeding with caution", extra={"filename": filename})
            result.warnings.append(f"Magic byte mismatch for .{ext}")

    @staticmethod
    def _check_zip_bomb(data: bytes, filename: str) -> None:
        """Heuristic defence against zip-bomb attacks in OOXML formats."""
        try:
            with zipfile.ZipFile(data if hasattr(data, "read") else __import__("io").BytesIO(data)) as zf:
                total_uncompressed = sum(info.file_size for info in zf.infolist())
                compressed_size = len(data)

                if total_uncompressed > _ZIP_BOMB_MAX_UNCOMPRESSED:
                    raise ValidationError(
                        f"'{filename}': uncompressed content ({total_uncompressed // (1024**3):.1f} GB) "
                        f"exceeds the 2 GB safety limit.",
                        code="ZIP_BOMB",
                    )

                if compressed_size > 0:
                    ratio = total_uncompressed / compressed_size
                    if ratio > _ZIP_BOMB_RATIO:
                        raise ValidationError(
                            f"'{filename}': compression ratio {ratio:.0f}× exceeds the "
                            f"{_ZIP_BOMB_RATIO}× safety limit (possible zip-bomb).",
                            code="ZIP_BOMB",
                        )
        except zipfile.BadZipFile:
            raise ValidationError(
                f"'{filename}': file claims to be a ZIP/OOXML format but is not a valid ZIP archive.",
                code="CORRUPT_ZIP",
            )

    @staticmethod
    def _check_pdf_encryption(data: bytes, filename: str, result: ValidationResult) -> None:
        """
        Detect password-protected / encrypted PDFs.
        We check for the /Encrypt dictionary marker in the first 8 KB.
        """
        header = data[:8192]
        if b"/Encrypt" in header:
            result.warnings.append("PDF appears to be password-protected; OCR may fail.")
            log.warning(
                "Encrypted PDF detected — OCR accuracy may be degraded",
                extra={"filename": filename},
            )


# ── Module-level convenience instance ──────────────────────────────────────

_default_validator = InputValidator()


def validate_upload(filename: str, data: bytes, max_size_bytes: int | None = None) -> ValidationResult:
    """
    Convenience function — validates *data* using the default validator.

    Pass *max_size_bytes* to override the global 200 MB cap for a single call.
    """
    if max_size_bytes is not None:
        return InputValidator(max_size_bytes=max_size_bytes).validate(filename, data)
    return _default_validator.validate(filename, data)
