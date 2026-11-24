"""通用响应归一化：从各家 JSON 中提取文本/区块。

适配器优先使用自己的映射；当结构不确定时，可调用本模块做兜底提取，
也支持通过 settings.text_path（点分路径，如 "result.words_block_list.0.words"）精确抽取。
"""
import json


def get_by_path(obj, path: str):
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _unwrap_body(js):
    """解开二次编码的响应体。

    图聆云 UOCR（``/tuling/uocr/v2/recognize``）把真正的结果再序列化进
    ``body`` 字符串里，形如 ``{"state":{...},"body":"{\\"pages\\":[...]}"}``。
    """
    if not isinstance(js, dict):
        return None
    body = js.get("body")
    if not isinstance(body, str):
        return None
    text = body.strip()
    if not text.startswith("{"):
        return None
    try:
        inner = json.loads(text)
    except Exception:
        return None
    return inner if isinstance(inner, dict) else None


def _quad_to_bbox(coord):
    """把 [{x,y},{x,y}...] 四边形转成 [x1,y1,x2,y2] 与扁平 polygon。"""
    if not isinstance(coord, list):
        return None, None
    pts = [(float(p["x"]), float(p["y"])) for p in coord if isinstance(p, dict) and "x" in p and "y" in p]
    if not pts:
        return None, None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    bbox = [min(xs), min(ys), max(xs), max(ys)]
    polygon: list[float] = []
    for x, y in pts:
        polygon.extend([x, y])
    return bbox, polygon or None


def _extract_pages(js: dict):
    """``pages[].lines[].content`` 结构（图聆云 / 通用文档识别）。"""
    pages = js.get("pages")
    if not isinstance(pages, list) or not pages:
        return None
    lines: list[str] = []
    blocks: list[dict] = []
    for pi, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        for ln in page.get("lines") or []:
            if not isinstance(ln, dict):
                continue
            txt = ln.get("content") or ln.get("text") or ""
            txt = str(txt)
            if not txt.strip():
                continue
            bbox, polygon = _quad_to_bbox(ln.get("coord"))
            conf = ln.get("conf")
            lines.append(txt)
            blocks.append(
                {
                    "type": "text",
                    "text": txt,
                    "bbox": bbox,
                    "polygon": polygon,
                    "confidence": float(conf) if isinstance(conf, (int, float)) else None,
                    "line_id": pi,
                    "index": len(blocks),
                }
            )
    if not blocks:
        return None
    text = "\n".join(lines)
    return {"text": text, "blocks": blocks, "pages": len(pages), "char_count": len(text)}


def extract(js: object, settings: dict | None = None) -> dict:
    settings = settings or {}

    # 1) 显式 text_path
    text_path = settings.get("text_path")
    if text_path:
        v = get_by_path(js, text_path)
        if isinstance(v, list):
            text = "\n".join(str(x) for x in v)
        else:
            text = str(v or "")
        blocks = [{"type": "text", "text": t} for t in (text.splitlines() or [text])]
        return {"text": text, "blocks": blocks, "pages": 1, "char_count": len(text)}

    # 2) 二次编码的响应体（图聆云 UOCR 的 body 字段）
    inner = _unwrap_body(js)
    if inner is not None:
        js = inner

    # 3) pages[].lines[].content
    if isinstance(js, dict):
        hit = _extract_pages(js)
        if hit:
            return hit

    # 4) 常见结构
    blocks: list[dict] = []
    lines: list[str] = []
    if isinstance(js, dict):
        # 百度/通用：words_result[]
        wr = js.get("words_result")
        if isinstance(wr, list):
            for i, w in enumerate(wr):
                txt = w.get("words") if isinstance(w, dict) else str(w)
                lines.append(txt)
                blocks.append({"type": "text", "text": txt, "index": i})
        # 华为：result.words_block_list[]
        res = js.get("result")
        if isinstance(res, dict) and isinstance(res.get("words_block_list"), list):
            for i, b in enumerate(res["words_block_list"]):
                txt = b.get("words") or b.get("text") or ""
                lines.append(txt)
                blocks.append({"type": "text", "text": txt, "index": i})
        # 讯飞-like：result.page_content
        if not lines and isinstance(res, dict) and res.get("page_content"):
            txt = res["page_content"]
            lines.append(txt)
            blocks.append({"type": "text", "text": txt, "index": 0})
        # 直接 text/content
        if not lines:
            for key in ("text", "content", "full_text", "fullText"):
                if isinstance(js.get(key), str) and js[key].strip():
                    txt = js[key]
                    lines.append(txt)
                    blocks.append({"type": "text", "text": txt, "index": 0})
                    break

    text = "\n".join(lines)
    return {"text": text, "blocks": blocks, "pages": 1, "char_count": len(text)}
