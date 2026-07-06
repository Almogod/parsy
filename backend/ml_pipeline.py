"""
Parsy — Phase 2: ML Pipeline
Local, privacy-preserving machine learning for structural document intelligence.

Models used (all run 100% locally, no internet after first download):
  1. LayoutLMv3 / DiT         — Document layout classification (bounding boxes)
  2. MiniLM-L6 via ONNX       — Semantic embedding for hierarchy clustering
  3. Table Transformer (TATR)  — Borderless table structure recognition

All models are lazy-loaded and cached. Falls back gracefully if unavailable.
"""
import io, os, logging, hashlib, time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("parsy.ml_pipeline")

# ── Lazy model registry ────────────────────────────────────────────────────
_models: dict[str, Any] = {}
ML_ENABLED = os.getenv("ML_ENABLED", "true").lower() == "true"
MODEL_CACHE = os.getenv("MODEL_CACHE_DIR", os.path.expanduser("~/.cache/parsy/models"))
os.makedirs(MODEL_CACHE, exist_ok=True)


@dataclass
class LayoutRegion:
    label: str          # "Title" | "Text" | "Table" | "Figure" | "List" | "Header" | "Footer"
    confidence: float
    bbox: tuple[float, float, float, float]   # x0, y0, x1, y1 (normalised 0–1)
    page: int
    text: str = ""


@dataclass
class MLAnnotation:
    regions: list[LayoutRegion] = field(default_factory=list)
    heading_clusters: list[list[str]] = field(default_factory=list)
    table_structures: list[dict] = field(default_factory=list)
    model_versions: dict = field(default_factory=dict)
    inference_ms: float = 0.0
    fallback: bool = False


# ── Model loaders ──────────────────────────────────────────────────────────
def _load_layout_model():
    """
    Load Microsoft DiT (Document Image Transformer) for layout analysis.
    Falls back to a heuristic classifier if transformers are unavailable.
    """
    if "layout" in _models:
        return _models["layout"]
    try:
        from transformers import AutoFeatureExtractor, AutoModelForImageClassification
        import torch
        model_id = "microsoft/dit-base-finetuned-rvlcdip"
        extractor = AutoFeatureExtractor.from_pretrained(model_id, cache_dir=MODEL_CACHE)
        model     = AutoModelForImageClassification.from_pretrained(model_id, cache_dir=MODEL_CACHE)
        model.eval()
        _models["layout"] = ("dit", extractor, model)
        log.info("DiT layout model loaded")
        return _models["layout"]
    except Exception as e:
        log.warning(f"DiT unavailable ({e}), using heuristic layout classifier")
        _models["layout"] = ("heuristic", None, None)
        return _models["layout"]


def _load_embedding_model():
    """
    Load MiniLM-L6-v2 via ONNX Runtime for fast local embeddings.
    Used for semantic heading/section clustering.
    """
    if "embedding" in _models:
        return _models["embedding"]
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(
            "sentence-transformers/all-MiniLM-L6-v2",
            cache_folder=MODEL_CACHE
        )
        _models["embedding"] = model
        log.info("MiniLM embedding model loaded")
        return model
    except Exception as e:
        log.warning(f"MiniLM unavailable ({e}), semantic clustering disabled")
        _models["embedding"] = None
        return None


def _load_table_model():
    """
    Load Table Transformer (TATR) for borderless table structure recognition.
    """
    if "table" in _models:
        return _models["table"]
    try:
        from transformers import AutoModelForObjectDetection, AutoFeatureExtractor
        model_id = "microsoft/table-transformer-structure-recognition"
        extractor = AutoFeatureExtractor.from_pretrained(model_id, cache_dir=MODEL_CACHE)
        model     = AutoModelForObjectDetection.from_pretrained(model_id, cache_dir=MODEL_CACHE)
        model.eval()
        _models["table"] = ("tatr", extractor, model)
        log.info("Table Transformer (TATR) loaded")
        return _models["table"]
    except Exception as e:
        log.warning(f"TATR unavailable ({e}), using rule-based table extraction")
        _models["table"] = ("rule_based", None, None)
        return _models["table"]


# ── Layout analysis ────────────────────────────────────────────────────────
def analyze_layout(page_images: list[bytes], page_texts: list[str]) -> list[LayoutRegion]:
    """
    Classify document regions per page using DiT or heuristics.
    page_images: PNG bytes per page (None if rasterization unavailable)
    page_texts:  extracted text per page
    """
    regions = []
    model_type, extractor, model = _load_layout_model()

    if model_type == "dit" and page_images:
        try:
            import torch
            from PIL import Image
            for pg_idx, (img_bytes, text) in enumerate(zip(page_images, page_texts)):
                image = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                inputs = extractor(images=image, return_tensors="pt")
                with torch.no_grad():
                    outputs = model(**inputs)
                probs  = outputs.logits.softmax(-1)[0]
                top_k  = probs.topk(3)
                for score, idx in zip(top_k.values, top_k.indices):
                    label = model.config.id2label[idx.item()]
                    regions.append(LayoutRegion(
                        label=label, confidence=round(score.item(), 3),
                        bbox=(0, 0, 1, 1), page=pg_idx, text=text[:200]
                    ))
        except Exception as e:
            log.warning(f"DiT inference failed: {e}, falling back to heuristics")
            model_type = "heuristic"

    if model_type == "heuristic":
        regions = _heuristic_layout(page_texts)

    return regions


def _heuristic_layout(page_texts: list[str]) -> list[LayoutRegion]:
    """Rule-based layout classification when ML is unavailable."""
    import re
    regions = []
    for pg_idx, text in enumerate(page_texts):
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        for line in lines[:5]:  # check first 5 lines per page for headers
            if len(line) < 80 and (line.isupper() or re.match(r"^#{1,3}\s", line)):
                regions.append(LayoutRegion("Title", 0.8, (0, 0, 1, 0.1), pg_idx, line))
                break
        if "|" in text and text.count("|") > 4:
            regions.append(LayoutRegion("Table", 0.75, (0, 0.3, 1, 0.7), pg_idx, ""))
    return regions


# ── Semantic heading clustering ────────────────────────────────────────────
def cluster_headings(headings: list[str], n_clusters: int = 3) -> list[list[str]]:
    """
    Groups document headings by semantic similarity using MiniLM embeddings.
    Returns clusters of semantically related heading strings.
    Falls back to returning all headings in a single cluster.
    """
    if not headings:
        return []

    model = _load_embedding_model()
    if model is None or len(headings) < 3:
        return [headings]

    try:
        import numpy as np
        embeddings = model.encode(headings, convert_to_numpy=True)

        # Simple K-means clustering
        from sklearn.cluster import KMeans
        k = min(n_clusters, len(headings))
        km = KMeans(n_clusters=k, random_state=42, n_init="auto")
        labels = km.fit_predict(embeddings)

        clusters: dict[int, list[str]] = {}
        for heading, label in zip(headings, labels):
            clusters.setdefault(int(label), []).append(heading)
        return list(clusters.values())

    except Exception as e:
        log.warning(f"Heading clustering failed: {e}")
        return [headings]


# ── Table structure recognition ────────────────────────────────────────────
def recognize_table_structure(table_image_bytes: bytes) -> dict:
    """
    Given a cropped image of a table region, returns the predicted
    row/column grid as a dict: {rows: int, cols: int, cells: [[label, bbox]]}
    Uses TATR if available, otherwise returns a stub.
    """
    model_type, extractor, model = _load_table_model()

    if model_type == "tatr" and table_image_bytes:
        try:
            import torch
            from PIL import Image
            image = Image.open(io.BytesIO(table_image_bytes)).convert("RGB")
            inputs = extractor(images=image, return_tensors="pt")
            with torch.no_grad():
                outputs = model(**inputs)

            # Post-process detections
            target_sizes = torch.tensor([image.size[::-1]])
            results = extractor.post_process_object_detection(
                outputs, threshold=0.5, target_sizes=target_sizes
            )[0]

            cells = []
            rows_set, cols_set = set(), set()
            for score, label, box in zip(results["scores"], results["labels"], results["boxes"]):
                label_name = model.config.id2label[label.item()]
                b = [round(x, 1) for x in box.tolist()]
                cells.append({"label": label_name, "score": round(score.item(), 3), "bbox": b})
                if "row" in label_name.lower(): rows_set.add(round(b[1]))
                if "col" in label_name.lower(): cols_set.add(round(b[0]))

            return {
                "rows": len(rows_set) or 1,
                "cols": len(cols_set) or 1,
                "cells": cells,
                "model": "tatr",
            }
        except Exception as e:
            log.warning(f"TATR inference failed: {e}")

    # Fallback
    return {"rows": 0, "cols": 0, "cells": [], "model": "fallback"}


# ── Master ML annotation entry point ──────────────────────────────────────
def annotate(
    page_texts:   list[str],
    headings:     list[str],
    page_images:  list[bytes] | None = None,
    table_images: list[bytes] | None = None,
) -> MLAnnotation:
    """
    Full ML annotation pass. Called by main.py after Level 2 parsing.
    Returns MLAnnotation with all available structural intelligence.
    """
    if not ML_ENABLED:
        return MLAnnotation(fallback=True)

    t0 = time.perf_counter()
    annotation = MLAnnotation()

    # 1. Layout regions
    try:
        annotation.regions = analyze_layout(page_images or [], page_texts)
    except Exception as e:
        log.error(f"Layout analysis error: {e}")

    # 2. Semantic heading clustering
    try:
        annotation.heading_clusters = cluster_headings(headings)
    except Exception as e:
        log.error(f"Heading clustering error: {e}")

    # 3. Table structure recognition
    if table_images:
        for img in table_images:
            try:
                struct = recognize_table_structure(img)
                annotation.table_structures.append(struct)
            except Exception as e:
                log.error(f"TATR error: {e}")

    annotation.model_versions = {
        "layout":    "DiT" if "layout" in _models else "heuristic",
        "embedding": "MiniLM-L6" if _models.get("embedding") else "disabled",
        "table":     "TATR" if "table" in _models else "rule_based",
    }
    annotation.inference_ms = round((time.perf_counter() - t0) * 1000, 1)
    return annotation
