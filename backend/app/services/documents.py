import base64
import csv
import io
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import fitz
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import ChunkEmbedding, Document, DocumentChunk
from app.services.rag import embed_text

HEADING_PATTERN = re.compile(
    r"^(?:#{1,6}\s+.+|\d+(?:\.\d+){0,3}\.?\s+.+|[一二三四五六七八九十]+[、.]\s*.+)$"
)
PAGE_NUMBER_PATTERN = re.compile(r"^(?:第\s*\d+\s*页|\d+\s*/\s*\d+|\d+)$")
TOC_PATTERN = re.compile(r".+\.{3,}\s*\d+$")

# 支持的文本 / 表格 / 网页 / 图片格式
TEXT_SUFFIXES = {".md", ".txt"}
OFFICE_SUFFIXES = {".docx", ".csv", ".html", ".htm"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
SUPPORTED_SUFFIXES = {".pdf", *TEXT_SUFFIXES, *OFFICE_SUFFIXES, *IMAGE_SUFFIXES}

IMAGE_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


@dataclass
class StructuredChunk:
    content: str
    section_title: str
    char_start: int
    char_end: int
    content_type: str = "body"


# --------------------------------------------------------------------------- #
# 视觉模型（扫描 PDF OCR / 图片理解）
# --------------------------------------------------------------------------- #
def _vision_client() -> tuple[str, str, str] | None:
    """返回 (base_url, api_key, model)。未启用视觉或未配置时返回 None。"""
    base_url = settings.vision_base_url or settings.llm_base_url
    api_key = settings.vision_api_key or settings.llm_api_key
    model = settings.vision_model or settings.llm_model
    if not settings.vision_enabled or not api_key or not base_url or not model:
        return None
    return base_url.rstrip("/"), api_key, model


def _vision_chat(data_uri: str, prompt: str) -> str:
    """调用 OpenAI 兼容视觉模型，返回文本结果。"""
    import httpx

    client = _vision_client()
    if not client:
        return ""
    base_url, api_key, model = client
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }
        ],
    }
    response = httpx.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=120,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def _downscale_image(data: bytes, mime: str) -> tuple[bytes, str]:
    """限制图片长边，返回 (压缩后字节, mime)。"""
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(data)).convert("RGB")
        max_side = settings.ocr_image_max_side
        if max(image.size) > max_side:
            image.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue(), "image/png"
    except Exception:  # noqa: BLE001
        return data, mime


def _ocr_pdf_page(page) -> str:
    """将无文本层的 PDF 页面渲染为图像并交给视觉模型转录文字。"""
    client = _vision_client()
    if not client:
        return ""
    pixmap = page.get_pixmap(dpi=150)
    png = pixmap.tobytes("png")
    data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    prompt = (
        "这是一页扫描文档图像。请完整、逐字转录图中所有文字，保留段落与列表结构；"
        "若含表格，按行列保留。只输出转录内容，不要添加任何解释或评论。"
    )
    try:
        return _vision_chat(data_uri, prompt)
    except Exception:  # noqa: BLE001
        return ""


def _extract_image(path: Path) -> list[tuple[int | None, str]]:
    """图片文件：交给视觉模型理解并转录文字。"""
    suffix = path.suffix.lower().lstrip(".")
    data, mime = _downscale_image(path.read_bytes(), IMAGE_MIME.get(suffix, "image/png"))
    client = _vision_client()
    if not client:
        return [(None, "[图片资料：未配置视觉模型，无法理解图片内容]")]
    data_uri = "data:{};base64,{}".format(
        mime, base64.b64encode(data).decode("ascii")
    )
    prompt = (
        "这是一张企业资料图片。请识别并转录图中所有文字；同时用简洁中文概括图中的内容、"
        "结构或要点。若图中有表格，请按行列转录。只输出内容，不要添加解释。"
    )
    try:
        text = _vision_chat(data_uri, prompt)
        return [(None, text or "[图片资料：视觉模型未返回可转录内容]")]
    except Exception as exc:  # noqa: BLE001
        return [(None, f"[图片资料：视觉模型解析失败 - {exc}]")]


# --------------------------------------------------------------------------- #
# 分格式抽取文本
# --------------------------------------------------------------------------- #
def extract_pdf_tables(page) -> str:
    """基于 PDF 词坐标的启发式表格重建：按行/列聚类，输出制表符分隔的行。"""
    words = page.get_text("words")  # (x0,y0,x1,y1,word,block,line,word_no)
    if len(words) < 4:
        return ""
    words.sort(key=lambda w: (round(w[1] / 4.0), w[0]))
    lines: list[list[tuple]] = []
    current: list[tuple] = []
    last_y = None
    for w in words:
        if last_y is None or abs(w[1] - last_y) <= 3:
            current.append(w)
        else:
            if current:
                lines.append(current)
            current = [w]
        last_y = w[1]
    if current:
        lines.append(current)

    rows: list[str] = []
    for line in lines:
        line.sort(key=lambda w: w[0])
        cells: list[str] = []
        prev_x1: float | None = None
        for w in line:
            text = w[4]
            if prev_x1 is not None and (w[0] - prev_x1) > 8:
                cells.append("\t")
            cells.append(text)
            prev_x1 = w[1]
        row = "".join(cells).strip()
        if row:
            rows.append(row)
    tab_rows = [row for row in rows if "\t" in row]
    return "\n".join(tab_rows) if len(tab_rows) >= 2 else ""


def _extract_pdf_pages(path: Path) -> list[tuple[int | None, str]]:
    pages: list[tuple[int | None, str]] = []
    with fitz.open(path) as pdf:
        for index, page in enumerate(pdf):
            text = page.get_text()
            if settings.ocr_enabled and len(text.strip()) < settings.ocr_min_chars:
                ocr_text = _ocr_pdf_page(page)
                text = ocr_text or text
            table_text = extract_pdf_tables(page)
            combined = "\n\n".join(part for part in (text, table_text) if part.strip())
            pages.append((index + 1, combined))
    return pages


def _extract_docx(path: Path) -> list[tuple[int | None, str]]:
    import docx  # python-docx

    doc = docx.Document(str(path))
    parts: list[str] = []
    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text.strip())
    for table in doc.tables:
        parts.append("[表格]")
        for row in table.rows:
            cells = [cell.text.replace("\n", " ").strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return [(None, "\n".join(parts))]


def _extract_csv(path: Path) -> list[tuple[int | None, str]]:
    rows: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        for row in csv.reader(handle):
            cells = [str(cell).strip() for cell in row]
            if any(cells):
                rows.append(" | ".join(cells))
    return [(None, "\n".join(rows))]


def _extract_html(path: Path) -> list[tuple[int | None, str]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    parts: list[str] = []
    for table in soup.find_all("table"):
        parts.append("[表格]")
        for tr in table.find_all("tr"):
            cells = [" ".join(td.get_text(" ", strip=True).split()) for td in tr.find_all(["td", "th"])]
            if any(cells):
                parts.append(" | ".join(cells))
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        text = heading.get_text(" ", strip=True)
        if text:
            parts.append(f"# {text}")
    body = soup.get_text("\n", strip=True)
    if body:
        parts.append(body)
    return [(None, "\n\n".join(parts))]


def extract_pages(path: Path) -> list[tuple[int | None, str]]:
    """按文件后缀分发抽取。返回 [(页码或 None, 文本)]。"""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_pages(path)
    if suffix in {".docx"}:
        return _extract_docx(path)
    if suffix in {".csv"}:
        return _extract_csv(path)
    if suffix in {".html", ".htm"}:
        return _extract_html(path)
    if suffix in IMAGE_SUFFIXES:
        return _extract_image(path)
    return [(None, path.read_text(encoding="utf-8", errors="ignore"))]


# --------------------------------------------------------------------------- #
# 文本清洗与结构化切片（保持原有逻辑）
# --------------------------------------------------------------------------- #
def normalized_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def repeated_margin_lines(pages: list[tuple[int | None, str]]) -> set[str]:
    if len(pages) < 2:
        return set()
    counts: Counter[str] = Counter()
    for _, text in pages:
        lines = [normalized_line(line) for line in text.splitlines() if normalized_line(line)]
        for line in set(lines[:3] + lines[-3:]):
            if 4 <= len(line) <= 160:
                counts[line] += 1
    threshold = max(2, int(len(pages) * 0.6))
    return {line for line, count in counts.items() if count >= threshold}


def clean_page_text(text: str, repeated_lines: set[str]) -> str:
    kept = []
    for raw_line in text.splitlines():
        line = normalized_line(raw_line)
        if (
            not line
            or line in repeated_lines
            or PAGE_NUMBER_PATTERN.fullmatch(line)
            or ("资料采集日期" in line and re.search(r"第\s*\d+\s*页", line))
        ):
            continue
        if TOC_PATTERN.fullmatch(line):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def is_heading(line: str) -> bool:
    return bool(HEADING_PATTERN.fullmatch(line)) and len(line) <= 100


def split_long_block(text: str, size: int, overlap: int) -> list[tuple[str, int, int]]:
    if len(text) <= size:
        return [(text, 0, len(text))]
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = max(text.rfind("\n", start, end), text.rfind("。", start, end))
            if boundary > start + size // 2:
                end = boundary + 1
        chunks.append((text[start:end].strip(), start, end))
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def structured_split(text: str, size: int = 700, overlap: int = 100) -> list[StructuredChunk]:
    cleaned = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not cleaned:
        return []
    sections: list[tuple[str, list[str]]] = []
    title = ""
    body: list[str] = []
    for line in cleaned.splitlines():
        if is_heading(line):
            if body:
                sections.append((title, body))
            title, body = line.lstrip("# ").strip(), []
        else:
            body.append(line)
    if body or title:
        sections.append((title, body))

    chunks: list[StructuredChunk] = []
    cursor = 0
    for section_title, lines in sections:
        section_text = "\n".join(lines).strip()
        if not section_text:
            continue
        prefix = f"{section_title}\n" if section_title else ""
        for content, local_start, local_end in split_long_block(section_text, size, overlap):
            full_content = f"{prefix}{content}".strip()
            chunks.append(
                StructuredChunk(
                    content=full_content,
                    section_title=section_title,
                    char_start=cursor + local_start,
                    char_end=cursor + local_end,
                )
            )
        cursor += len(section_text) + 1
    return chunks


def is_short_noise(content: str) -> bool:
    if len(content) >= 60:
        return False
    return (
        "资料采集日期" in content
        or content.startswith("来源：")
        or "官方来源目录" in content
        or bool(re.search(r"https?://", content))
    )


def split_text(text: str, size: int = 800, overlap: int = 120) -> list[str]:
    return [chunk.content for chunk in structured_split(text, size, overlap)]


# --------------------------------------------------------------------------- #
# 文档处理主流程
# --------------------------------------------------------------------------- #
def process_document(document_id: int, db: Session) -> None:
    document = db.get(Document, document_id)
    if not document:
        return
    try:
        document.status = "processing"
        document.error_message = ""
        document.claimed_at = None
        document.chunks.clear()
        db.flush()
        pages = extract_pages(Path(document.file_path))
        repeated_lines = repeated_margin_lines(pages)
        index = 0
        for page_number, page_text in pages:
            cleaned = clean_page_text(page_text, repeated_lines)
            for item in structured_split(cleaned, settings.chunk_size, settings.chunk_overlap):
                if is_short_noise(item.content):
                    continue
                chunk = DocumentChunk(
                    document_id=document.id,
                    content=item.content,
                    chunk_index=index,
                    page_number=page_number,
                    section_title=item.section_title,
                    char_start=item.char_start,
                    char_end=item.char_end,
                    content_type=item.content_type,
                )
                db.add(chunk)
                db.flush()
                vector = embed_text(item.content)
                db.add(
                    ChunkEmbedding(
                        chunk_id=chunk.id,
                        vector_json=json.dumps(vector),
                        vector=vector,
                        model=settings.embedding_model,
                    )
                )
                index += 1
        if index == 0:
            raise ValueError("No readable text was extracted")
        document.status = "ready"
    except Exception as exc:
        document.status = "failed"
        document.error_message = str(exc)[:1000]
    document.claimed_at = None
    db.commit()
