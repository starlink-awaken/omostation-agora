"""tools_bos: ocr domain — multimodal OCR & layout-faithful restoration (BET-Y1Q4-T2-03).

Local-only OCR pipeline for scanned red-header official documents:
  1. recognize text boxes via macOS Vision framework (pyobjc, lazy import;
     zero network, zero upload — BET non_goal red line), with a stub source
     for non-macOS / CI environments.
  2. reconstruct document layout from pure geometry: line clustering,
     column grouping, table detection, seal anchoring, handwriting metadata.
  3. render layout-faithful Markdown.

CLI (verify contract):
  python -m agora.server.tools_bos.ocr test_document_layout   # offline self-test
  python -m agora.server.tools_bos.ocr extract --file <path>  # real extraction
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = "agora.bos.ocr.v1"
MAX_CANVAS_PIXELS = 16_000_000  # circuit_breaker: larger images get tiled
TILE_GRID = 2  # split into TILE_GRID x TILE_GRID blocks when oversized
LINE_OVERLAP_RATIO = 0.5  # vertical overlap fraction to merge boxes into a line
COLUMN_GAP_EM = 3.0  # x-gap (in avg glyph widths) that separates columns
TABLE_MIN_ROWS = 3  # >=3 aligned rows to call a table
TABLE_X_TOLERANCE = 12  # px tolerance for cross-row column alignment
LOW_CONFIDENCE = 0.55  # below this a box is handwriting / seal candidate
SEAL_CLUSTER_RADIUS = 180  # px radius for low-confidence clustering → seal
SEAL_LEXICON = ("专用章", "之印", "公章", "印章", "盖章", "戳记")  # stamp wording
HANDWRITING_MAX_LEN = 12  # handwritten annotations are short strokes


@dataclass(frozen=True, slots=True)
class TextBox:
    """One recognized text fragment with its bounding box (origin top-left)."""

    text: str
    x: float
    y: float
    w: float
    h: float
    confidence: float = 1.0
    kind: str = "body"  # body | heading | table_cell | handwriting | seal | meta


@dataclass
class LayoutLine:
    """A visual line: boxes sharing vertical overlap, ordered by x."""

    boxes: list[TextBox] = field(default_factory=list)
    column: int = 0

    @property
    def y(self) -> float:
        return min(b.y for b in self.boxes)

    @property
    def x0(self) -> float:
        return min(b.x for b in self.boxes)

    @property
    def x1(self) -> float:
        return max(b.x + b.w for b in self.boxes)

    @property
    def text(self) -> str:
        return "  ".join(b.text for b in self.boxes)


@dataclass
class DocumentLayout:
    """Structured layout output feeding render_markdown."""

    heading: str = ""
    columns: list[list[LayoutLine]] = field(default_factory=list)  # per column
    meta_lines: list[str] = field(default_factory=list)  # doc number / date row
    tables: list[list[list[str]]] = field(default_factory=list)  # rows of cells
    seals: list[dict[str, Any]] = field(default_factory=list)
    handwriting: list[dict[str, Any]] = field(default_factory=list)
    body: list[str] = field(default_factory=list)


# ── Recognition sources ───────────────────────────────────────────────


def _load_cgimage(image_path: str | Path):
    """Load a CGImageRef via CGImageSource (CFURL handler bridging proved flaky)."""
    import Quartz  # type: ignore[import-not-found]

    url = Quartz.CFURLCreateFromFileSystemRepresentation(
        None, bytes(str(image_path), "utf-8"), len(str(image_path)), False
    )
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    return Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)


def _recognize_via_vision(image_path: str | Path) -> list[TextBox]:
    """macOS Vision framework OCR (zh-Hans). Lazy pyobjc import; raises on absence."""
    import Quartz  # type: ignore[import-not-found]
    from Vision import VNImageRequestHandler, VNRecognizeTextRequest  # type: ignore[import-not-found]

    img = _load_cgimage(image_path)
    handler = VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
    request = VNRecognizeTextRequest.alloc().initWithCompletionHandler_(None)
    request.setRecognitionLanguages_(["zh-Hans", "en-US"])
    request.setRecognitionLevel_(0)  # accurate
    request.setUsesLanguageCorrection_(True)
    handler.performRequests_error_([request], None)

    img_w = float(Quartz.CGImageGetWidth(img))
    img_h = float(Quartz.CGImageGetHeight(img))
    boxes: list[TextBox] = []
    for obs in request.results() or []:
        top = obs.topCandidates_(1)
        if not top:
            continue
        cand = top[0]
        bb = obs.boundingBox()  # normalized, origin bottom-left
        boxes.append(
            TextBox(
                text=str(cand.string()),
                x=bb.origin.x * img_w,
                y=(1 - bb.origin.y - bb.size.height) * img_h,
                w=bb.size.width * img_w,
                h=bb.size.height * img_h,
                confidence=float(cand.confidence()),
            )
        )
    return boxes


def _image_dimensions(image_path: str | Path) -> tuple[float, float]:
    """Image pixel size via Quartz CGImage (no PIL dependency needed on macOS)."""
    import Quartz  # type: ignore[import-not-found]

    img = _load_cgimage(image_path)
    return float(Quartz.CGImageGetWidth(img)), float(Quartz.CGImageGetHeight(img))


def _recognize_stub(document: dict[str, Any]) -> list[TextBox]:
    """Synthetic source for tests / non-macOS: boxes come from a fixture dict."""
    return [
        TextBox(
            text=b["text"],
            x=b["x"],
            y=b["y"],
            w=b["w"],
            h=b["h"],
            confidence=b.get("confidence", 1.0),
        )
        for b in document.get("boxes", [])
    ]


def _tile_image(image_path: str | Path) -> list[tuple[Path, float, float]]:
    """Split an oversized bitmap into TILE_GRID² crops via Quartz (no new deps).

    Returns (tile_path, x_offset, y_offset) with top-left-origin offsets —
    CG coordinates are bottom-left, so tile row i maps to (grid-1-i) top-down.
    """
    import Quartz  # type: ignore[import-not-found]

    img = _load_cgimage(image_path)
    w, h = float(Quartz.CGImageGetWidth(img)), float(Quartz.CGImageGetHeight(img))
    tiles: list[tuple[Path, float, float]] = []
    import tempfile

    tmpdir = Path(tempfile.mkdtemp(prefix="ocr-tiles-"))
    for i in range(TILE_GRID):
        for j in range(TILE_GRID):
            tw, th = w / TILE_GRID, h / TILE_GRID
            # CG origin is bottom-left: row i (bottom-up) = top-down row grid-1-i
            rect = Quartz.CGRectMake(j * tw, i * th, tw, th)
            cropped = Quartz.CGImageCreateWithImageInRect(img, rect)
            out = tmpdir / f"tile-{i}-{j}.png"
            out_url = Quartz.CFURLCreateFromFileSystemRepresentation(
                None, bytes(str(out), "utf-8"), len(str(out)), False
            )
            dest = Quartz.CGImageDestinationCreateWithURL(
                out_url, Quartz.kUTTypePNG, 1, None
            )
            Quartz.CGImageDestinationAddImage(dest, cropped, None)
            Quartz.CGImageDestinationFinalize(dest)
            tiles.append((out, j * tw, (TILE_GRID - 1 - i) * th))
    return tiles


def recognize(
    image_path: str | Path | None = None, document: dict[str, Any] | None = None
) -> list[TextBox]:
    """Pick a recognition source; circuit_breaker tiles oversized images."""
    if image_path is not None:
        try:
            w, h = _image_dimensions(image_path)
        except ImportError:
            if document is None:
                document = {"boxes": []}
            return _recognize_stub(document)
        if w * h > MAX_CANVAS_PIXELS:
            # circuit_breaker: oversized → tile, recognize per block, stitch coords
            stitched: list[TextBox] = []
            for tile_path, dx, dy in _tile_image(image_path):
                for box in _recognize_via_vision(tile_path):
                    stitched.append(
                        TextBox(
                            text=box.text,
                            x=box.x + dx,
                            y=box.y + dy,
                            w=box.w,
                            h=box.h,
                            confidence=box.confidence,
                        )
                    )
            return stitched
        return _recognize_via_vision(image_path)
    return _recognize_stub(document or {"boxes": []})


# ── Layout engine (pure geometry, zero deps, fully offline testable) ──


def cluster_lines(boxes: list[TextBox]) -> list[LayoutLine]:
    """Merge boxes whose vertical spans overlap >= LINE_OVERLAP_RATIO."""
    lines: list[LayoutLine] = []
    for box in sorted(boxes, key=lambda b: (b.y, b.x)):
        for line in lines:
            overlap = _v_overlap(box, line)
            if (
                overlap >= LINE_OVERLAP_RATIO * box.h
                and overlap >= LINE_OVERLAP_RATIO * _line_h(line)
            ):
                line.boxes.append(box)
                break
        else:
            lines.append(LayoutLine(boxes=[box]))
    for line in lines:
        line.boxes.sort(key=lambda b: b.x)
    lines.sort(key=lambda ln: (ln.y, ln.x0))
    return lines


def _v_overlap(box: TextBox, line: LayoutLine) -> float:
    ly0, ly1 = min(b.y for b in line.boxes), max(b.y + b.h for b in line.boxes)
    return max(0.0, min(box.y + box.h, ly1) - max(box.y, ly0))


def _line_h(line: LayoutLine) -> float:
    return max(b.h for b in line.boxes) or 1.0


def _glyph_width(lines: list[LayoutLine]) -> float:
    """Median glyph width estimate: median line span / ~20 glyphs."""
    spans = sorted(ln.x1 - ln.x0 for ln in lines)
    return max((spans[len(spans) // 2] or 1.0) / 20.0, 1.0)


def detect_meta_lines(lines: list[LayoutLine]) -> list[LayoutLine]:
    """Red-header meta band: first rows whose boxes have a wide x-gap.

    The doc-number / date row lands as one visual line with two distant boxes
    (gap >= COLUMN_GAP_EM glyph widths) — that band is the two-column meta row.
    """
    threshold = COLUMN_GAP_EM * _glyph_width(lines) if lines else 0.0
    meta: list[LayoutLine] = []
    for line in lines[:3]:  # meta band sits right under the heading
        gaps = [b.x - (a.x + a.w) for a, b in zip(line.boxes, line.boxes[1:])]
        if len(line.boxes) >= 2 and gaps and max(gaps) >= threshold:
            meta.append(line)
    return meta


def detect_tables(lines: list[LayoutLine]) -> tuple[list[list[list[str]]], set[int]]:
    """Consecutive lines with >=2 boxes whose x-edges align within tolerance."""
    tables: list[list[list[str]]] = []
    used: set[int] = set()
    run: list[LayoutLine] = []
    for idx, line in enumerate(lines):
        if len(line.boxes) >= 2 and _aligned_with_previous(line, run):
            run.append(line)
        else:
            if len(run) >= TABLE_MIN_ROWS:
                tables.append([[b.text for b in ln.boxes] for ln in run])
                used.update(range(idx - len(run), idx))
            run = [line] if len(line.boxes) >= 2 else []
    if len(run) >= TABLE_MIN_ROWS:
        start = len(lines) - len(run)
        tables.append([[b.text for b in ln.boxes] for ln in run])
        used.update(range(start, len(lines)))
    return tables, used


def _aligned_with_previous(line: LayoutLine, run: list[LayoutLine]) -> bool:
    if not run:
        return True
    prev = run[-1]
    if len(prev.boxes) != len(line.boxes):
        return False
    return all(
        abs(a.x - b.x) < TABLE_X_TOLERANCE for a, b in zip(prev.boxes, line.boxes)
    )


def detect_seals(boxes: list[TextBox]) -> tuple[list[dict[str, Any]], set[TextBox]]:
    """Seal anchor: spatial cluster of low-confidence boxes, or stamp-lexicon hits.

    Real stamps often yield only ONE legible fragment (red round background
    kills OCR confidence), so a lexicon match (专用章/之印/...) alone anchors
    a seal. Returns (seal metadata, member boxes) for pipeline exclusion.
    """
    low = [
        b
        for b in boxes
        if b.confidence < LOW_CONFIDENCE or any(w in b.text for w in SEAL_LEXICON)
    ]
    clusters: list[list[TextBox]] = []
    for box in low:
        for cluster in clusters:
            cx = sum(b.x + b.w / 2 for b in cluster) / len(cluster)
            cy = sum(b.y + b.h / 2 for b in cluster) / len(cluster)
            if (
                (box.x + box.w / 2 - cx) ** 2 + (box.y + box.h / 2 - cy) ** 2
            ) ** 0.5 <= SEAL_CLUSTER_RADIUS:
                cluster.append(box)
                break
        else:
            clusters.append([box])
    seals: list[dict[str, Any]] = []
    members: set[TextBox] = set()
    for cluster in clusters:
        if len(cluster) < 2 and not any(
            any(w in b.text for w in SEAL_LEXICON) for b in cluster
        ):
            continue  # single low-conf box without stamp wording: not a seal
        members.update(cluster)
        x0 = min(b.x for b in cluster)
        y0 = min(b.y for b in cluster)
        seals.append(
            {
                "type": "seal",
                "bbox": [
                    x0,
                    y0,
                    max(b.x + b.w for b in cluster) - x0,
                    max(b.y + b.h for b in cluster) - y0,
                ],
                "nearby_text": "".join(
                    b.text for b in sorted(cluster, key=lambda b: (b.y, b.x))
                ),
                "confidence": round(
                    sum(b.confidence for b in cluster) / len(cluster), 3
                ),
            }
        )
    return seals, members


def classify_handwriting(
    boxes: list[TextBox],
) -> tuple[list[dict[str, Any]], set[TextBox]]:
    """Low-confidence, short, non-seal fragments → handwritten annotations."""
    out: list[dict[str, Any]] = []
    members: set[TextBox] = set()
    for b in boxes:
        if b.confidence >= LOW_CONFIDENCE or len(b.text) > HANDWRITING_MAX_LEN:
            continue
        if any(w in b.text for w in SEAL_LEXICON):
            continue  # stamp wording belongs to seals, not handwriting
        members.add(b)
        out.append(
            {
                "type": "handwriting",
                "bbox": [b.x, b.y, b.w, b.h],
                "text": b.text,
                "confidence": round(b.confidence, 3),
            }
        )
    return out, members


def build_layout(boxes: list[TextBox]) -> DocumentLayout:
    """Full pipeline: seal/handwriting anchoring first, then lines → meta → tables."""
    layout = DocumentLayout()
    layout.seals, seal_members = detect_seals(boxes)
    normal = [b for b in boxes if b not in seal_members]
    layout.handwriting, hw_members = classify_handwriting(normal)
    body_boxes = [b for b in normal if b not in hw_members]

    lines = cluster_lines(body_boxes)
    meta_lines = detect_meta_lines(lines)
    layout.meta_lines = [ln.text for ln in meta_lines]
    rest = [ln for ln in lines if ln not in meta_lines]
    if rest:
        layout.heading = rest[0].text if _looks_like_heading(rest[0]) else ""
        rest = rest[1:] if layout.heading else rest
    tables, used = detect_tables(rest)
    layout.tables = tables
    layout.body = [ln.text for i, ln in enumerate(rest) if i not in used]
    layout.columns = [
        lines
    ]  # single reading order; column split only marks the meta band
    return layout


def _looks_like_heading(line: LayoutLine) -> bool:
    return line.x1 - line.x0 > 200 and len(line.boxes) <= 6


# ── Markdown rendering ────────────────────────────────────────────────


def render_markdown(layout: DocumentLayout) -> str:
    out: list[str] = []
    if layout.heading:
        out.append(f"# {layout.heading}")
        out.append("")
    for meta in layout.meta_lines:
        out.append(f"> {meta}")
    if layout.meta_lines:
        out.append("")
    for para in layout.body:
        out.append(para)
        out.append("")
    for table in layout.tables:
        if table:
            out.append("| " + " | ".join(table[0]) + " |")
            out.append("|" + "---|" * len(table[0]))
            for row in table[1:]:
                out.append("| " + " | ".join(row) + " |")
            out.append("")
    for seal in layout.seals:
        out.append(
            f"**印章锚点** bbox={[round(v, 1) for v in seal['bbox']]} text=`{seal['nearby_text']}`"
        )
    for hw in layout.handwriting:
        out.append(
            f"**手写签批** bbox={[round(v, 1) for v in hw['bbox']]} text=`{hw['text']}`"
        )
    if layout.seals or layout.handwriting:
        out.append("")
    return "\n".join(out).strip()


# ── BOS service entry & CLI ───────────────────────────────────────────


def extract(file_path: str | Path) -> dict[str, Any]:
    """Extract a scanned document into structured JSON + layout Markdown."""
    path = Path(file_path)
    boxes = recognize(
        image_path=path
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".heic"}
        else None
    )
    layout = build_layout(boxes)
    return {
        "schema": SCHEMA,
        "file": str(path),
        "boxes": [asdict(b) for b in boxes],
        "layout": {
            "heading": layout.heading,
            "meta_lines": layout.meta_lines,
            "tables": layout.tables,
            "seals": layout.seals,
            "handwriting": layout.handwriting,
            "body": layout.body,
        },
        "markdown": render_markdown(layout),
    }


async def bos_ocr_extract(file_path: str) -> dict[str, Any]:
    """BOS tool: local OCR extraction (bos://perception/agora/ocr)."""
    from agora.server._response import FORMAT_VERSION, _error, _ok

    try:
        result = extract(file_path)
        return _ok({"format_version": FORMAT_VERSION, "status": "ok", **result})
    except Exception as exc:  # pragma: no cover - bridge guard
        return _error(f"OCR extraction exception: {exc}")


def _stub_document() -> dict[str, Any]:
    """Synthetic red-header document covering every layout contract branch."""
    boxes = [
        # heading (wide, centered)
        {
            "text": "关于推进数字医疗健康服务的实施意见",
            "x": 240,
            "y": 60,
            "w": 720,
            "h": 48,
        },
        # meta band: doc number column | date column (two-column row)
        {"text": "国卫办发布〔2026〕15号", "x": 120, "y": 160, "w": 320, "h": 30},
        {"text": "2026年8月30日", "x": 640, "y": 160, "w": 200, "h": 30},
        # body paragraphs
        {
            "text": "各省、自治区、直辖市卫生健康委：",
            "x": 110,
            "y": 240,
            "w": 420,
            "h": 32,
        },
        {
            "text": "为深入贯彻落实健康中国战略部署，现就推进",
            "x": 110,
            "y": 300,
            "w": 640,
            "h": 32,
        },
        {
            "text": "数字医疗健康服务提出以下实施意见，请结合实际执行。",
            "x": 110,
            "y": 350,
            "w": 700,
            "h": 32,
        },
        # table 4x3 (aligned x edges)
        {"text": "试点地区", "x": 150, "y": 440, "w": 130, "h": 28},
        {"text": "覆盖医院", "x": 350, "y": 440, "w": 130, "h": 28},
        {"text": "完成时限", "x": 550, "y": 440, "w": 130, "h": 28},
        {"text": "北京市", "x": 150, "y": 490, "w": 130, "h": 28},
        {"text": "32家", "x": 350, "y": 490, "w": 130, "h": 28},
        {"text": "2026Q4", "x": 550, "y": 490, "w": 130, "h": 28},
        {"text": "上海市", "x": 150, "y": 540, "w": 130, "h": 28},
        {"text": "28家", "x": 350, "y": 540, "w": 130, "h": 28},
        {"text": "2027Q1", "x": 550, "y": 540, "w": 130, "h": 28},
        {"text": "广东省", "x": 150, "y": 590, "w": 130, "h": 28},
        {"text": "45家", "x": 350, "y": 590, "w": 130, "h": 28},
        {"text": "2027Q2", "x": 550, "y": 590, "w": 130, "h": 28},
        # seal cluster (low confidence, spatially tight)
        {
            "text": "国家卫生健康",
            "x": 700,
            "y": 680,
            "w": 90,
            "h": 26,
            "confidence": 0.41,
        },
        {
            "text": "委员会印章",
            "x": 712,
            "y": 716,
            "w": 82,
            "h": 26,
            "confidence": 0.38,
        },
        # handwritten approval (low confidence, isolated, short)
        {"text": "同意", "x": 180, "y": 700, "w": 60, "h": 34, "confidence": 0.44},
    ]
    return {"boxes": boxes}


def test_document_layout() -> dict[str, Any]:
    """Offline self-test over the synthetic red-header fixture (verify contract)."""
    boxes = recognize(document=_stub_document())
    layout = build_layout(boxes)
    checks = {
        "heading_found": "实施意见" in layout.heading,
        "meta_lines_found": len(layout.meta_lines) >= 1,
        "table_detected": len(layout.tables) == 1 and len(layout.tables[0]) == 4,
        "table_rows_restored": bool(layout.tables) and len(layout.tables[0][0]) == 3,
        "seal_anchored": len(layout.seals) == 1,
        "handwriting_anchored": len(layout.handwriting) == 1,
        "body_preserved": any("健康中国" in p for p in layout.body),
    }
    layout_score = round(sum(checks.values()) / len(checks), 3)
    return {
        "schema": SCHEMA,
        "checks": checks,
        "layout_fidelity": layout_score,
        "seals": layout.seals,
        "handwriting": layout.handwriting,
        "markdown": render_markdown(layout),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["test_document_layout", "extract"])
    parser.add_argument("--file", default=None, help="image / PDF path for extract")
    args = parser.parse_args(argv)

    if args.command == "test_document_layout":
        report = test_document_layout()
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0 if report["layout_fidelity"] >= 0.95 else 1

    if not args.file:
        parser.error("extract requires --file <path>")
    print(json.dumps(extract(args.file), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
