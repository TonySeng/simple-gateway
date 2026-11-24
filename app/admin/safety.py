"""安全审查管理：词库、规则、Key 绑定辅助与独立审查记录。"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_current_admin
from app.core.models import ApiKey, SafetyLibrary, SafetyPolicy, SafetyReview
from app.core.safety_bootstrap import (
    BUILTIN_LIBRARIES,
    BUILTIN_POLICIES,
    BUILTIN_VERSION,
    META_KEY,
    bootstrap_safety,
)
from app.core.models import Meta
from app.core.schemas import (
    SafetyBuiltinItem,
    SafetyBuiltinsRead,
    SafetyLibraryCreate,
    SafetyLibraryRead,
    SafetyLibraryUpdate,
    SafetyOverview,
    SafetyPolicyCreate,
    SafetyPolicyRead,
    SafetyPolicyUpdate,
    SafetyReviewItem,
    SafetyReviewPage,
    SafetyTestRequest,
    SafetyTestResult,
)
from app.services.llm import safety

router = APIRouter(prefix="/api/admin/safety", tags=["安全审查"])

MAX_TERMS = 10000
MAX_REGEX_LEN = 500


def _today_utc() -> datetime:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _term_lines(terms: str) -> list[str]:
    out: list[str] = []
    for raw in (terms or "").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _validate_terms(terms: str) -> int:
    """校验词库内容，返回有效词条数。正则写错直接拒绝保存。"""
    lines = _term_lines(terms)
    if len(lines) > MAX_TERMS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"单个词库最多 {MAX_TERMS} 条词，当前 {len(lines)} 条",
        )
    for i, line in enumerate(lines, start=1):
        if line[:3].lower() == "re:":
            pattern = line[3:].strip()
            if not pattern:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=f"第 {i} 条正则为空（格式应为 re:表达式）",
                )
            if len(pattern) > MAX_REGEX_LEN:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=f"第 {i} 条正则过长（上限 {MAX_REGEX_LEN} 字符）",
                )
            try:
                re.compile(pattern)
            except re.error as e:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=f"第 {i} 条正则无效：{e}",
                ) from e
    return len(lines)


def _to_library(lib: SafetyLibrary) -> SafetyLibraryRead:
    return SafetyLibraryRead(
        id=lib.id,
        name=lib.name,
        description=lib.description or "",
        terms=lib.terms or "",
        term_count=lib.term_count or 0,
        is_enabled=lib.is_enabled,
        is_builtin=bool(lib.is_builtin),
        builtin_key=lib.builtin_key or "",
        created_at=lib.created_at,
        updated_at=lib.updated_at,
    )


def _to_policy(db: Session, p: SafetyPolicy) -> SafetyPolicyRead:
    ids = [x for x in (p.library_ids or []) if x]
    libs = db.query(SafetyLibrary).filter(SafetyLibrary.id.in_(ids)).all() if ids else []
    by_id = {x.id: x for x in libs}
    names = [by_id[x].name for x in ids if x in by_id]
    bound = db.query(func.count(ApiKey.id)).filter(ApiKey.safety_policy_id == p.id).scalar() or 0
    return SafetyPolicyRead(
        id=p.id,
        name=p.name,
        description=p.description or "",
        is_enabled=p.is_enabled,
        is_default=p.is_default,
        library_ids=ids,
        library_names=names,
        input_enabled=p.input_enabled,
        output_enabled=p.output_enabled,
        match_mode=p.match_mode or "contains",
        case_sensitive=p.case_sensitive,
        normalize=p.normalize,
        threshold=p.threshold or 1,
        max_text_chars=p.max_text_chars or 200000,
        fail_closed=p.fail_closed,
        default_reply=p.default_reply or safety.DEFAULT_REPLY,
        bound_keys=int(bound),
        is_builtin=bool(p.is_builtin),
        builtin_key=p.builtin_key or "",
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def _to_review(r: SafetyReview) -> SafetyReviewItem:
    matched = r.matched_terms if isinstance(r.matched_terms, list) else []
    libs = r.library_names if isinstance(r.library_names, list) else []
    return SafetyReviewItem(
        id=r.id,
        request_id=r.request_id or "",
        api_key_id=r.api_key_id,
        key_name=r.key_name or "",
        policy_id=r.policy_id,
        policy_name=r.policy_name or "",
        library_names=libs,
        direction=r.direction or "input",
        action=r.action or "blocked",
        matched_terms=[x for x in matched if isinstance(x, dict)],
        text_excerpt=r.text_excerpt or "",
        model=r.model or "",
        endpoint=r.endpoint or "",
        is_stream=bool(r.is_stream),
        detail=r.detail or "",
        created_at=r.created_at,
    )


def _validate_library_ids(db: Session, ids: list[str]) -> None:
    if not ids:
        return
    found = {
        str(x[0])
        for x in db.query(SafetyLibrary.id).filter(SafetyLibrary.id.in_(ids)).all()
    }
    missing = [x for x in ids if x not in found]
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"引用的词库不存在：{', '.join(missing)}",
        )


def _clear_other_defaults(db: Session, keep_id: str | None = None) -> None:
    q = db.query(SafetyPolicy).filter(SafetyPolicy.is_default.is_(True))
    if keep_id:
        q = q.filter(SafetyPolicy.id != keep_id)
    for row in q.all():
        row.is_default = False


# ---------------------------------------------------------------- 词库
@router.get("/libraries", response_model=list[SafetyLibraryRead], summary="词库列表")
def list_libraries(db: Session = Depends(get_db), admin: str = Depends(get_current_admin)):
    rows = db.query(SafetyLibrary).order_by(SafetyLibrary.updated_at.desc()).all()
    return [_to_library(x) for x in rows]


@router.post("/libraries", response_model=SafetyLibraryRead, status_code=status.HTTP_201_CREATED,
             summary="新建词库")
def create_library(
    payload: SafetyLibraryCreate,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="词库名称不能为空")
    if db.query(SafetyLibrary).filter(SafetyLibrary.name == name).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="词库名称已存在")
    count = _validate_terms(payload.terms)
    lib = SafetyLibrary(
        name=name,
        description=payload.description.strip(),
        terms=payload.terms,
        term_count=count,
        is_enabled=payload.is_enabled,
    )
    db.add(lib)
    db.commit()
    db.refresh(lib)
    safety.invalidate_cache()
    return _to_library(lib)


@router.put("/libraries/{lid}", response_model=SafetyLibraryRead, summary="更新词库")
def update_library(
    lid: str,
    payload: SafetyLibraryUpdate,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    lib = db.query(SafetyLibrary).filter(SafetyLibrary.id == lid).first()
    if lib is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="词库不存在")
    data = payload.model_dump(exclude_unset=True)
    if "name" in data:
        name = str(data["name"] or "").strip()
        if not name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="词库名称不能为空")
        dup = (
            db.query(SafetyLibrary)
            .filter(SafetyLibrary.name == name, SafetyLibrary.id != lid)
            .first()
        )
        if dup:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="词库名称已存在")
        lib.name = name
    if "description" in data:
        lib.description = str(data["description"] or "").strip()
    if "terms" in data:
        lib.terms = str(data["terms"] or "")
        lib.term_count = _validate_terms(lib.terms)
    if "is_enabled" in data:
        lib.is_enabled = bool(data["is_enabled"])
    db.commit()
    db.refresh(lib)
    safety.invalidate_cache()
    return _to_library(lib)


@router.delete("/libraries/{lid}", status_code=status.HTTP_204_NO_CONTENT, summary="删除词库")
def delete_library(
    lid: str,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    lib = db.query(SafetyLibrary).filter(SafetyLibrary.id == lid).first()
    if lib is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="词库不存在")
    users = [
        p.name
        for p in db.query(SafetyPolicy).all()
        if lid in [x for x in (p.library_ids or [])]
    ]
    if users:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="该词库仍被审查规则引用，请先解除引用：" + "、".join(users),
        )
    db.delete(lib)
    db.commit()
    safety.invalidate_cache()
    return None


# ---------------------------------------------------------------- 规则
@router.get("/policies", response_model=list[SafetyPolicyRead], summary="审查规则列表")
def list_policies(db: Session = Depends(get_db), admin: str = Depends(get_current_admin)):
    rows = db.query(SafetyPolicy).order_by(SafetyPolicy.is_default.desc(), SafetyPolicy.updated_at.desc()).all()
    return [_to_policy(db, x) for x in rows]


@router.post("/policies", response_model=SafetyPolicyRead, status_code=status.HTTP_201_CREATED,
             summary="新建审查规则")
def create_policy(
    payload: SafetyPolicyCreate,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="规则名称不能为空")
    if db.query(SafetyPolicy).filter(SafetyPolicy.name == name).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="规则名称已存在")
    if payload.match_mode not in ("contains", "word"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="匹配方式只能是 contains / word")
    _validate_library_ids(db, payload.library_ids)

    # 第一条规则自动成为默认规则，避免「配置了规则但 Key 全都不生效」
    first = db.query(SafetyPolicy).count() == 0
    is_default = bool(payload.is_default or first)
    if is_default:
        _clear_other_defaults(db)
    p = SafetyPolicy(
        name=name,
        description=payload.description.strip(),
        is_enabled=payload.is_enabled,
        is_default=is_default,
        library_ids=payload.library_ids,
        input_enabled=payload.input_enabled,
        output_enabled=payload.output_enabled,
        match_mode=payload.match_mode,
        case_sensitive=payload.case_sensitive,
        normalize=payload.normalize,
        threshold=payload.threshold,
        max_text_chars=payload.max_text_chars,
        fail_closed=payload.fail_closed,
        default_reply=payload.default_reply or safety.DEFAULT_REPLY,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    safety.invalidate_cache(p.id)
    return _to_policy(db, p)


@router.put("/policies/{pid}", response_model=SafetyPolicyRead, summary="更新审查规则")
def update_policy(
    pid: str,
    payload: SafetyPolicyUpdate,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    p = db.query(SafetyPolicy).filter(SafetyPolicy.id == pid).first()
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="规则不存在")
    data = payload.model_dump(exclude_unset=True)

    if "name" in data:
        name = str(data["name"] or "").strip()
        if not name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="规则名称不能为空")
        dup = db.query(SafetyPolicy).filter(SafetyPolicy.name == name, SafetyPolicy.id != pid).first()
        if dup:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="规则名称已存在")
        p.name = name
    if "match_mode" in data and data["match_mode"] not in ("contains", "word"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="匹配方式只能是 contains / word")
    if "library_ids" in data:
        _validate_library_ids(db, list(data["library_ids"] or []))
        p.library_ids = list(data["library_ids"] or [])
    if data.get("is_default") is True:
        _clear_other_defaults(db, keep_id=pid)
    for field in (
        "description", "is_enabled", "is_default", "input_enabled", "output_enabled",
        "match_mode", "case_sensitive", "normalize", "threshold", "max_text_chars",
        "fail_closed", "default_reply",
    ):
        if field in data:
            setattr(p, field, data[field])
    if not p.default_reply:
        p.default_reply = safety.DEFAULT_REPLY
    db.commit()
    db.refresh(p)
    safety.invalidate_cache(p.id)
    return _to_policy(db, p)


@router.delete("/policies/{pid}", status_code=status.HTTP_204_NO_CONTENT, summary="删除审查规则")
def delete_policy(
    pid: str,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    p = db.query(SafetyPolicy).filter(SafetyPolicy.id == pid).first()
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="规则不存在")
    bound = db.query(func.count(ApiKey.id)).filter(ApiKey.safety_policy_id == pid).scalar() or 0
    if bound:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"仍有 {int(bound)} 个 API Key 绑定该规则，请先改为继承默认或其它规则",
        )
    was_default = p.is_default
    db.delete(p)
    db.commit()
    safety.invalidate_cache(pid)
    if was_default:
        nxt = (
            db.query(SafetyPolicy)
            .filter(SafetyPolicy.is_enabled.is_(True))
            .order_by(SafetyPolicy.updated_at.desc())
            .first()
        )
        if nxt is not None:
            nxt.is_default = True
            db.commit()
    return None


# ---------------------------------------------------------------- 内置模板
def _builtin_status(db: Session) -> SafetyBuiltinsRead:
    installed = db.query(Meta).filter(Meta.key == META_KEY).first()
    lib_rows = {
        r.builtin_key: r
        for r in db.query(SafetyLibrary).filter(SafetyLibrary.builtin_key != "").all()
    }
    pol_rows = {
        r.builtin_key: r
        for r in db.query(SafetyPolicy).filter(SafetyPolicy.builtin_key != "").all()
    }
    key_to_id = {k: r.id for k, r in lib_rows.items()}
    items: list[SafetyBuiltinItem] = []

    for spec in BUILTIN_LIBRARIES:
        row = lib_rows.get(spec["key"])
        modified = bool(
            row
            and (
                row.name != spec["name"]
                or row.description != spec["description"]
                or row.terms != spec["terms"]
                or not row.is_enabled
            )
        )
        items.append(SafetyBuiltinItem(
            key=spec["key"], name=spec["name"], kind="library",
            exists=row is not None, modified=modified,
        ))
    for spec in BUILTIN_POLICIES:
        row = pol_rows.get(spec["key"])
        expected = {key_to_id[k] for k in spec["libraries"] if k in key_to_id}
        modified = bool(
            row
            and (
                row.name != spec["name"]
                or row.description != spec["description"]
                or set(row.library_ids or []) != expected
                or row.input_enabled != spec["input_enabled"]
                or row.output_enabled != spec["output_enabled"]
                or row.match_mode != spec["match_mode"]
                or row.threshold != spec["threshold"]
                or row.default_reply != spec["default_reply"]
                or not row.is_enabled
            )
        )
        items.append(SafetyBuiltinItem(
            key=spec["key"], name=spec["name"], kind="policy",
            exists=row is not None, modified=modified,
        ))
    return SafetyBuiltinsRead(
        version=BUILTIN_VERSION,
        installed_version=installed.value if installed else "",
        items=items,
    )


@router.get("/builtins", response_model=SafetyBuiltinsRead, summary="内置词库与规则状态")
def get_builtins(db: Session = Depends(get_db), admin: str = Depends(get_current_admin)):
    return _builtin_status(db)


@router.post("/builtins/reinstall", summary="补齐缺失的内置项（不覆盖已有修改）")
def reinstall_builtins(db: Session = Depends(get_db), admin: str = Depends(get_current_admin)):
    return bootstrap_safety(db, reinstall=True)


@router.post("/builtins/restore", summary="恢复内置项为出厂内容（覆盖内置项修改）")
def restore_builtins(db: Session = Depends(get_db), admin: str = Depends(get_current_admin)):
    return bootstrap_safety(db, restore=True)


# ---------------------------------------------------------------- 测试与记录
@router.post("/test", response_model=SafetyTestResult, summary="用指定规则试审查一段文本")
def test_review(
    payload: SafetyTestRequest,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    if payload.direction not in (safety.DIRECTION_INPUT, safety.DIRECTION_OUTPUT):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="direction 只能是 input / output")
    if payload.policy_id:
        policy = db.query(SafetyPolicy).filter(SafetyPolicy.id == payload.policy_id).first()
    else:
        policy = (
            db.query(SafetyPolicy)
            .filter(SafetyPolicy.is_enabled.is_(True), SafetyPolicy.is_default.is_(True))
            .order_by(SafetyPolicy.updated_at.desc())
            .first()
        )
    if policy is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="没有可用的审查规则")
    r = safety.review_texts([payload.text], policy, payload.direction, db=db)
    return SafetyTestResult(
        blocked=r.blocked,
        matched=[{"term": m.get("term", ""), "library": m.get("library", "")} for m in r.matched],
        policy_name=policy.name,
        detail=r.detail,
        truncated=r.truncated,
    )


@router.get("/overview", response_model=SafetyOverview, summary="审查概况")
def overview(db: Session = Depends(get_db), admin: str = Depends(get_current_admin)):
    today = _today_utc()
    base = db.query(func.count(SafetyReview.id)).filter(SafetyReview.created_at >= today)
    return SafetyOverview(
        libraries=int(db.query(func.count(SafetyLibrary.id)).scalar() or 0),
        policies=int(db.query(func.count(SafetyPolicy.id)).scalar() or 0),
        reviews_today=int(base.scalar() or 0),
        blocked_input_today=int(
            base.filter(SafetyReview.direction == safety.DIRECTION_INPUT).scalar() or 0
        ),
        blocked_output_today=int(
            base.filter(SafetyReview.direction == safety.DIRECTION_OUTPUT).scalar() or 0
        ),
    )


@router.get("/records", response_model=SafetyReviewPage, summary="审查记录")
def list_records(
    policy_id: Optional[str] = None,
    key_id: Optional[str] = None,
    direction: Optional[str] = None,
    action: Optional[str] = None,
    request_id: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    page: int = 1,
    page_size: int = 30,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    page = max(1, page)
    page_size = min(max(page_size, 1), 200)
    filters = []
    if policy_id:
        filters.append(SafetyReview.policy_id == policy_id)
    if key_id:
        filters.append(SafetyReview.api_key_id == key_id)
    if direction:
        filters.append(SafetyReview.direction == direction)
    if action:
        filters.append(SafetyReview.action == action)
    if request_id:
        filters.append(SafetyReview.request_id == request_id)
    if start:
        filters.append(SafetyReview.created_at >= start.replace(tzinfo=None))
    if end:
        filters.append(SafetyReview.created_at <= end.replace(tzinfo=None))

    q = db.query(SafetyReview)
    count_q = db.query(func.count(SafetyReview.id))
    for f in filters:
        q = q.filter(f)
        count_q = count_q.filter(f)

    total = int(count_q.scalar() or 0)
    rows = (
        q.order_by(SafetyReview.created_at.desc(), SafetyReview.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return SafetyReviewPage(
        items=[_to_review(x) for x in rows],
        total=total,
        page=page,
        page_size=page_size,
    )
