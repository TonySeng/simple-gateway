"""请求参数校验错误的统一处理。

FastAPI 默认的 ``RequestValidationError`` 处理器会直接
``jsonable_encoder(exc.errors())`` 再返回。问题在于：

* 若 ``input`` 是二进制（典型场景：把 ``multipart/form-data`` 上传打到了
  只接受 JSON 的端点），``jsonable_encoder`` 会对 ``bytes`` 调 ``.decode()``，
  遇到非 UTF-8 字节即抛 ``UnicodeDecodeError``，最终把本应 422 的请求变成 **500**。

本模块做两件事：

1. **保证语义正确**：把不可序列化的值安全降级为可读描述，永远返回 422；
2. **让报错自解释**：针对最常见的两种调用姿势错误追加 ``hint``，
   使用方看响应就能知道该怎么改，不必来问。
"""
from __future__ import annotations

from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import Request

#: 按错误定位（loc）匹配的提示语
_HINTS_BY_LOC = {
    "body.file": (
        "该接口要求 multipart/form-data 上传且字段名必须为 `file`"
        "（不是 image / img / upload / data）。示例：curl -F \"file=@a.png\" ..."
    ),
    "body": (
        "该接口只接受 JSON 请求体（Content-Type: application/json）。"
        "若你要上传图片文件，请改用 /v1/ocr/recognize/file（multipart，字段名 file）；"
        "若要传 base64，请用 {\"image_base64\": \"...\"}。"
    ),
}

_DISPLAY_LIMIT = 500


def _safe(value: Any) -> Any:
    """把任意值降级为 JSON 可序列化、且人类可读的形式。"""
    if isinstance(value, (bytes, bytearray)):
        try:
            text = value.decode("utf-8")
        except Exception:
            return f"<binary {len(value)} bytes>"
        return text if len(text) <= _DISPLAY_LIMIT else text[:_DISPLAY_LIMIT] + "…"
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # 例如 ctx 里携带的异常对象
    try:
        return str(value)
    except Exception:  # pragma: no cover - 极端兜底
        return f"<{type(value).__name__}>"


def _hint_for(errors: list[dict]) -> str | None:
    for err in errors:
        loc = ".".join(str(x) for x in (err.get("loc") or ()))
        if loc in _HINTS_BY_LOC:
            return _HINTS_BY_LOC[loc]
    for err in errors:
        if err.get("type") in ("model_attributes_type", "dict_type", "json_invalid"):
            return _HINTS_BY_LOC["body"]
    return None


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    errors: list[dict] = []
    for err in exc.errors():
        item = dict(err)
        item.pop("url", None)  # 部分版本会带，与调用方无关
        if "input" in item:
            item["input"] = _safe(item["input"])
        if "ctx" in item:
            item["ctx"] = _safe(item["ctx"])
        errors.append(item)

    payload: dict[str, Any] = {"detail": jsonable_encoder(errors)}
    hint = _hint_for(errors)
    if hint:
        payload["hint"] = hint
    return JSONResponse(status_code=422, content=payload)
