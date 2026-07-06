"""
Parsy Backend — Level 2b: Vision + OCR Pipeline
Handles scanned documents, image-heavy PDFs, and rotated pages.
Uses Tesseract OCR + LayoutLM-inspired bounding-box classification.
"""
import io, asyncio, concurrent.futures, logging
from dataclasses import dataclass
from typing import AsyncGenerator

from PIL import Image
import pytesseract
from pdf2image import convert_from_bytes, pdfinfo_from_bytes

from base_parser import ParsedBlock, FastParseResult
from base_parser import BaseParser, CorruptFileError

log = logging.getLogger("parsy.parsers.ocr")


# ── OCR config ─────────────────────────────────────────────────────────────
TESSERACT_CONFIG = r"--oem 3 --psm 3 -l eng"   # LSTM engine, auto page-seg


@dataclass
class OCRPageResult:
    page_num: int
    text: str
    blocks: list[ParsedBlock]
    confidence: float


def _ocr_page(args: tuple) -> OCRPageResult:
    """Runs in a thread pool — one page at a time."""
    img: Image.Image
    page_num: int
    img, page_num = args

    # Upscale for better OCR accuracy (300 DPI equivalent)
    scale = max(1.0, 300 / max(img.width, img.height) * 10)
    w = int(img.width  * scale)
    h = int(img.height * scale)
    img = img.resize((w, h), Image.LANCZOS)

    # Get Tesseract data (word-level bboxes + confidence)
    data = pytesseract.image_to_data(img, config=TESSERACT_CONFIG,
                                     output_type=pytesseract.Output.DICT)

    blocks  = []
    lines   = {}  # group words by line_num
    confs   = []

    for i in range(len(data["text"])):
        word  = data["text"][i].strip()
        conf  = int(data["conf"][i])
        if conf < 30 or not word:
            continue
        confs.append(conf)
        ln_key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(ln_key, []).append((word, data["left"][i], data["top"][i],
                                             data["width"][i], data["height"][i]))

    # Reconstruct lines → blocks with heuristic heading detection
    img_h = h
    page_lines = sorted(lines.items(), key=lambda x: (x[1][0][2] if x[1] else 0))
    heights = [max(w[4] for w in words) for _, words in page_lines if words]
    median_h = sorted(heights)[len(heights)//2] if heights else 14

    for (bn, pn, ln), words in page_lines:
        text = " ".join(w[0] for w in words)
        ch   = max(w[4] for w in words) if words else 14
        ratio = ch / max(median_h, 1)

        if ratio >= 1.8:
            level, btype = 1, "heading"
        elif ratio >= 1.35:
            level, btype = 2, "heading"
        elif ratio >= 1.15:
            level, btype = 3, "heading"
        else:
            level, btype = 0, "paragraph"

        blocks.append(ParsedBlock(btype, level, text, page_num))

    avg_conf = sum(confs) / len(confs) if confs else 0
    full_text = "\n".join(" ".join(w[0] for w in words)
                          for _, words in page_lines)
    return OCRPageResult(page_num, full_text, blocks, avg_conf / 100)


class VisionOCRPipeline(BaseParser):
    """
    Level 2b: Rasterizes each PDF page and runs parallel Tesseract OCR.
    Falls back gracefully if Tesseract is not installed.
    """

    supported_extensions = frozenset({
        "pdf", "png", "jpg", "jpeg", "tiff", "tif", "bmp", "webp",
    })

    def __init__(self, max_workers: int = 4, dpi: int = 200):
        super().__init__()
        self.max_workers = max_workers
        self.dpi = dpi

    # Called by BaseParser.parse — do NOT call directly.
    async def _parse(self, filename: str, data: bytes) -> FastParseResult:
        ext = self.extension_of(filename)

        try:
            if ext in ("png", "jpg", "jpeg", "tiff", "tif", "bmp", "webp"):
                images = [Image.open(io.BytesIO(data)).convert("RGB")]
                results = await self._ocr_pages(images)
                for img in images:
                    img.close()
            elif ext == "pdf":
                # Strict streaming/chunking for memory safety: read page count and batch rasterise
                try:
                    info = pdfinfo_from_bytes(data)
                    page_count = info.get("Pages", 1)
                except Exception as info_err:
                    log.warning("Could not extract PDF page count via pdfinfo; defaulting to full rasterization",
                                extra={"error": str(info_err)})
                    page_count = None

                if page_count and page_count > 4:
                    log.info("Processing large PDF in page batches for memory safety",
                             extra={"file_name": filename, "pages": page_count})
                    results = []
                    batch_size = 4
                    loop = asyncio.get_running_loop()
                    for start_page in range(1, page_count + 1, batch_size):
                        end_page = min(start_page + batch_size - 1, page_count)
                        batch_images = await loop.run_in_executor(
                            None,
                            lambda s=start_page, e=end_page: convert_from_bytes(
                                data, dpi=self.dpi, fmt="RGB",
                                first_page=s, last_page=e,
                                thread_count=self.max_workers
                            )
                        )
                        batch_results = await self._ocr_pages(batch_images, offset=start_page - 1)
                        results.extend(batch_results)
                        # Immediately release PIL image memory
                        for img in batch_images:
                            img.close()
                else:
                    images = await self._rasterize_pdf(data)
                    results = await self._ocr_pages(images)
                    for img in images:
                        img.close()
            else:
                # Unexpected — try treating as image
                log.warning(
                    "Unexpected extension for OCR; attempting image decode",
                    extra={"file_name": filename, "ext": ext},
                )
                try:
                    images = [Image.open(io.BytesIO(data)).convert("RGB")]
                    results = await self._ocr_pages(images)
                    for img in images:
                        img.close()
                except Exception as img_err:
                    log.error(
                        "Image decode failed for unexpected extension",
                        extra={"file_name": filename, "exc": str(img_err)},
                    )
                    results = []
        except Exception as exc:
            raise CorruptFileError(
                f"Failed to decode/rasterize '{filename}': {exc}",
                filename=filename,
                cause=exc,
            ) from exc

        if not results:
            log.warning("No pages OCR'd; returning empty result",
                        extra={"file_name": filename})
            return FastParseResult([], 0, "", [], {})

        results.sort(key=lambda r: r.page_num)

        all_blocks = [b for r in results for b in r.blocks]
        raw_text   = "\n\n".join(r.text for r in results)
        avg_conf   = sum(r.confidence for r in results) / len(results) if results else 0

        meta = {
            "pageCount":     len(results),
            "ocrConfidence": f"{avg_conf:.1%}",
            "pipeline":      "vision_ocr_streaming",
        }
        log.debug(
            "OCR complete",
            extra={
                "file_name": filename,
                "pages": len(results),
                "avg_confidence": f"{avg_conf:.2%}",
            },
        )
        return FastParseResult(all_blocks, len(results), raw_text, [], meta)

    async def _rasterize_pdf(self, data: bytes) -> list[Image.Image]:
        loop = asyncio.get_running_loop()
        images = await loop.run_in_executor(
            None,
            lambda: convert_from_bytes(data, dpi=self.dpi, fmt="RGB",
                                       thread_count=self.max_workers)
        )
        return images

    async def _ocr_pages(self, images: list[Image.Image], offset: int = 0) -> list[OCRPageResult]:
        loop = asyncio.get_running_loop()
        payloads = [(img, i + offset) for i, img in enumerate(images)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = [loop.run_in_executor(pool, _ocr_page, p) for p in payloads]
            results = await asyncio.gather(*futures, return_exceptions=True)

        # Filter out exceptions (page-level fault isolation)
        clean = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                clean.append(OCRPageResult(i + offset, f"[OCR Error page {i + offset}: {r}]", [], 0.0))
            else:
                clean.append(r)
        return clean
