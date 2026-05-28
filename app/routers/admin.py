from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import APIKeyPrincipal, is_super_admin, require_admin_principal
from app.db.engine import get_sessionmaker
from app.db.models import APIKey, AuditLog, Organization, utc_now

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
)
logger = logging.getLogger("ai_serving.audit")

GroupBy = Literal["key", "model", "org"]
CostString = Annotated[str, Field(pattern=r"^\d+\.\d{6}$")]


class UsageGroup(BaseModel):
    group_key: str
    total_tokens: int
    total_prompt_tokens: int
    total_completion_tokens: int
    cost_usd: CostString
    request_count: int
    error_count: int


class UsageWindow(BaseModel):
    since: datetime
    until: datetime
    group_by: GroupBy


class UsageResponse(BaseModel):
    groups: list[UsageGroup]
    window: UsageWindow


class AuditLogItem(BaseModel):
    id: str
    request_id: str
    ts: datetime
    org_id: str | None
    api_key_id: str | None
    principal_hash: str | None
    provider: str | None
    model: str | None
    status_code: int
    error_type: str | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: CostString
    latency_ms: int
    stream: bool


class AuditResponse(BaseModel):
    items: list[AuditLogItem]
    next_offset: int | None


class OrganizationCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Organization name cannot be blank")
        return value


class OrganizationResponse(BaseModel):
    id: str
    name: str
    created_at: datetime


class APIKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(min_length=1)
    org_id: str

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("API key name cannot be blank")
        return value

    @field_validator("scopes")
    @classmethod
    def normalize_scopes(cls, value: list[str]) -> list[str]:
        scopes: list[str] = []
        seen: set[str] = set()
        for item in value:
            scope = item.strip()
            if scope and scope not in seen:
                scopes.append(scope)
                seen.add(scope)
        if not scopes:
            raise ValueError("At least one scope is required")
        return scopes


class APIKeyCreateResponse(BaseModel):
    id: str
    prefix: str
    name: str
    scopes: list[str]
    org_id: str
    created_at: datetime
    api_key: str


class APIKeyListItem(BaseModel):
    id: str
    prefix: str
    name: str
    scopes: list[str]
    org_id: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class APIKeyListResponse(BaseModel):
    items: list[APIKeyListItem]


class APIKeyRevokeResponse(BaseModel):
    id: str
    revoked_at: datetime


class AuditReplayResponse(BaseModel):
    replayed: int
    failed: int


def _require_sessionmaker(
    sessionmaker: async_sessionmaker[AsyncSession] | None,
) -> async_sessionmaker[AsyncSession]:
    if sessionmaker is None:
        raise HTTPException(status_code=503, detail="Database is not initialized")
    return sessionmaker


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _optional_as_utc(value: datetime | None) -> datetime | None:
    return _as_utc(value) if value is not None else None


def _format_cost(value: Decimal | int | float | None) -> str:
    decimal_value = Decimal("0") if value is None else Decimal(value)
    return f"{decimal_value.quantize(Decimal('0.000001')):.6f}"


def _split_scopes(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _require_super_admin(principal: APIKeyPrincipal) -> None:
    if not is_super_admin(principal):
        raise HTTPException(status_code=403, detail="super_admin scope is required")


def _principal_org_id(principal: APIKeyPrincipal) -> str:
    if principal.org_id is None:
        raise HTTPException(status_code=403, detail="Admin principal is not org-scoped")
    return principal.org_id


def _key_item(api_key: APIKey) -> APIKeyListItem:
    return APIKeyListItem(
        id=api_key.id,
        prefix=api_key.prefix,
        name=api_key.name,
        scopes=_split_scopes(api_key.scopes),
        org_id=api_key.org_id,
        created_at=_as_utc(api_key.created_at),
        last_used_at=_optional_as_utc(api_key.last_used_at),
        revoked_at=_optional_as_utc(api_key.revoked_at),
    )


def _audit_fields_from_json(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Audit fallback line must be a JSON object")

    allowed = {column.name for column in AuditLog.__table__.columns}
    fields = {key: field_value for key, field_value in value.items() if key in allowed}
    if "cost_usd" in fields:
        fields["cost_usd"] = Decimal(str(fields["cost_usd"]))
    if isinstance(fields.get("ts"), str):
        fields["ts"] = datetime.fromisoformat(fields["ts"].replace("Z", "+00:00"))
    return fields


def _append_audit_quarantine(path: Path, lines: list[str]) -> None:
    if not lines:
        return

    failed_path = path.with_name(path.name + ".failed")
    failed_path.parent.mkdir(parents=True, exist_ok=True)
    with open(failed_path, "a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line.rstrip("\r\n"))
            handle.write("\n")


@router.get("/usage", response_model=UsageResponse)
async def get_usage(
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    group_by: GroupBy = Query(default="key"),
    principal: APIKeyPrincipal = Depends(require_admin_principal),
    sessionmaker=Depends(get_sessionmaker),
) -> UsageResponse:
    now = datetime.now(timezone.utc)
    since = _as_utc(since) if since is not None else now - timedelta(hours=24)
    until = _as_utc(until) if until is not None else now
    if since > until:
        raise HTTPException(status_code=400, detail="since must be before or equal to until")

    sessionmaker = _require_sessionmaker(sessionmaker)

    if group_by == "key":
        group_expr = func.coalesce(AuditLog.api_key_id, AuditLog.principal_hash, "unknown")
    elif group_by == "model":
        group_expr = func.coalesce(AuditLog.model, "unknown")
    else:
        group_expr = func.coalesce(AuditLog.org_id, "unknown")

    filters = [AuditLog.ts >= since, AuditLog.ts <= until]
    if not is_super_admin(principal):
        filters.append(AuditLog.org_id == _principal_org_id(principal))

    stmt = (
        select(
            group_expr.label("group_key"),
            func.coalesce(func.sum(AuditLog.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(AuditLog.prompt_tokens), 0).label("total_prompt_tokens"),
            func.coalesce(func.sum(AuditLog.completion_tokens), 0).label(
                "total_completion_tokens"
            ),
            func.coalesce(func.sum(AuditLog.cost_usd), 0).label("cost_usd"),
            func.count(AuditLog.id).label("request_count"),
            func.coalesce(
                func.sum(case((AuditLog.status_code >= 400, 1), else_=0)),
                0,
            ).label("error_count"),
        )
        .where(*filters)
        .group_by(group_expr)
        .order_by(group_expr)
    )

    async with sessionmaker() as session:
        rows = (await session.execute(stmt)).all()

    groups = [
        UsageGroup(
            group_key=str(row.group_key),
            total_tokens=int(row.total_tokens or 0),
            total_prompt_tokens=int(row.total_prompt_tokens or 0),
            total_completion_tokens=int(row.total_completion_tokens or 0),
            cost_usd=_format_cost(row.cost_usd),
            request_count=int(row.request_count or 0),
            error_count=int(row.error_count or 0),
        )
        for row in rows
    ]
    return UsageResponse(
        groups=groups,
        window=UsageWindow(since=since, until=until, group_by=group_by),
    )


@router.get("/audit", response_model=AuditResponse)
async def list_audit_logs(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    model: str | None = Query(default=None),
    api_key_id: str | None = Query(default=None),
    principal: APIKeyPrincipal = Depends(require_admin_principal),
    sessionmaker=Depends(get_sessionmaker),
) -> AuditResponse:
    sessionmaker = _require_sessionmaker(sessionmaker)

    stmt = select(AuditLog).order_by(AuditLog.ts.desc()).offset(offset).limit(limit)
    scoped_org_id: str | None = None
    if not is_super_admin(principal):
        scoped_org_id = _principal_org_id(principal)
        stmt = stmt.where(AuditLog.org_id == scoped_org_id)
    if since is not None:
        stmt = stmt.where(AuditLog.ts >= _as_utc(since))
    if until is not None:
        stmt = stmt.where(AuditLog.ts <= _as_utc(until))
    if model is not None:
        stmt = stmt.where(AuditLog.model == model)
    if api_key_id is not None:
        stmt = stmt.where(AuditLog.api_key_id == api_key_id)

    async with sessionmaker() as session:
        if api_key_id is not None and scoped_org_id is not None:
            api_key = await session.get(APIKey, api_key_id)
            if api_key is None or api_key.org_id != scoped_org_id:
                raise HTTPException(status_code=404, detail="API key not found")
        rows = (await session.execute(stmt)).scalars().all()

    items = [
        AuditLogItem(
            id=row.id,
            request_id=row.request_id,
            ts=_as_utc(row.ts),
            org_id=row.org_id,
            api_key_id=row.api_key_id,
            principal_hash=row.principal_hash,
            provider=row.provider,
            model=row.model,
            status_code=row.status_code,
            error_type=row.error_type,
            prompt_tokens=row.prompt_tokens,
            completion_tokens=row.completion_tokens,
            total_tokens=row.total_tokens,
            cost_usd=_format_cost(row.cost_usd),
            latency_ms=row.latency_ms,
            stream=row.stream,
        )
        for row in rows
    ]
    next_offset = offset + limit if len(items) == limit else None
    return AuditResponse(items=items, next_offset=next_offset)


@router.post("/orgs", response_model=OrganizationResponse)
async def create_organization(
    body: OrganizationCreateRequest,
    principal: APIKeyPrincipal = Depends(require_admin_principal),
    sessionmaker=Depends(get_sessionmaker),
) -> OrganizationResponse:
    _require_super_admin(principal)
    sessionmaker = _require_sessionmaker(sessionmaker)
    async with sessionmaker() as session:
        org = Organization(name=body.name)
        session.add(org)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="Organization already exists") from exc
        await session.refresh(org)
        return OrganizationResponse(id=org.id, name=org.name, created_at=_as_utc(org.created_at))


@router.post("/keys", response_model=APIKeyCreateResponse)
async def create_api_key(
    body: APIKeyCreateRequest,
    principal: APIKeyPrincipal = Depends(require_admin_principal),
    sessionmaker=Depends(get_sessionmaker),
) -> APIKeyCreateResponse:
    sessionmaker = _require_sessionmaker(sessionmaker)
    token = "sk-" + secrets.token_urlsafe(32)
    scopes_list = list(body.scopes)
    if is_super_admin(principal):
        org_id = body.org_id
    else:
        org_id = _principal_org_id(principal)
        if body.org_id != org_id:
            raise HTTPException(status_code=403, detail="Cannot create API key for another org")
        scopes_list = [scope for scope in scopes_list if scope != "super_admin"]
        if not scopes_list:
            raise HTTPException(status_code=400, detail="At least one non-super_admin scope is required")
    scopes = ",".join(scopes_list)

    async with sessionmaker() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=404, detail="Organization not found")

        api_key = APIKey(
            org_id=org_id,
            prefix=token[:12],
            key_hash=_hash_token(token),
            name=body.name,
            scopes=scopes,
        )
        session.add(api_key)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="API key already exists") from exc
        await session.refresh(api_key)

    return APIKeyCreateResponse(
        id=api_key.id,
        prefix=api_key.prefix,
        name=api_key.name,
        scopes=scopes_list,
        org_id=api_key.org_id,
        created_at=_as_utc(api_key.created_at),
        api_key=token,
    )


@router.post("/keys/{key_id}/revoke", response_model=APIKeyRevokeResponse)
async def revoke_api_key(
    key_id: str,
    principal: APIKeyPrincipal = Depends(require_admin_principal),
    sessionmaker=Depends(get_sessionmaker),
) -> APIKeyRevokeResponse:
    sessionmaker = _require_sessionmaker(sessionmaker)
    async with sessionmaker() as session:
        api_key = await session.get(APIKey, key_id)
        if api_key is None:
            raise HTTPException(status_code=404, detail="API key not found")
        if not is_super_admin(principal) and api_key.org_id != _principal_org_id(principal):
            raise HTTPException(status_code=404, detail="API key not found")
        api_key.revoked_at = utc_now()
        await session.commit()
        await session.refresh(api_key)
        return APIKeyRevokeResponse(id=api_key.id, revoked_at=_as_utc(api_key.revoked_at))


@router.get("/keys", response_model=APIKeyListResponse)
async def list_api_keys(
    org_id: str | None = Query(default=None),
    include_revoked: bool = Query(default=False),
    principal: APIKeyPrincipal = Depends(require_admin_principal),
    sessionmaker=Depends(get_sessionmaker),
) -> APIKeyListResponse:
    """Non-super admins are always scoped to their own org; org_id query is ignored."""

    sessionmaker = _require_sessionmaker(sessionmaker)
    stmt = select(APIKey).order_by(APIKey.created_at.desc())
    if is_super_admin(principal):
        effective_org_id = org_id
    else:
        effective_org_id = _principal_org_id(principal)
    if effective_org_id is not None:
        stmt = stmt.where(APIKey.org_id == effective_org_id)
    if not include_revoked:
        stmt = stmt.where(APIKey.revoked_at.is_(None))

    async with sessionmaker() as session:
        api_keys = (await session.execute(stmt)).scalars().all()

    return APIKeyListResponse(items=[_key_item(api_key) for api_key in api_keys])


@router.post("/audit/replay", response_model=AuditReplayResponse)
async def replay_audit_fallback(
    request: Request,
    principal: APIKeyPrincipal = Depends(require_admin_principal),
    sessionmaker=Depends(get_sessionmaker),
) -> AuditReplayResponse:
    _require_super_admin(principal)
    sessionmaker = _require_sessionmaker(sessionmaker)

    settings = request.app.state.settings
    path = Path(settings.audit_fallback_path)
    if not path.exists():
        return AuditReplayResponse(replayed=0, failed=0)

    snapshot_path = path.with_name(path.name + f".replay-{uuid4().hex}")
    if not path.exists():
        return AuditReplayResponse(replayed=0, failed=0)
    try:
        os.replace(path, snapshot_path)
    except FileNotFoundError:
        return AuditReplayResponse(replayed=0, failed=0)
    except OSError as exc:
        logger.warning(
            "Audit fallback replay snapshot unavailable",
            extra={"extra_fields": {"audit_fallback_path": str(path)}},
            exc_info=True,
        )
        raise HTTPException(
            status_code=503,
            detail="Audit replay temporarily unavailable",
        ) from exc

    replayed = 0
    failed = 0
    lines: list[str] = []
    next_line_index = 0
    quarantine_lines: list[str] = []
    try:
        lines = snapshot_path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            next_line_index = index
            if not line.strip():
                next_line_index = index + 1
                continue
            try:
                fields = _audit_fields_from_json(json.loads(line))
                async with sessionmaker() as session:
                    session.add(AuditLog(**fields))
                    await session.commit()
                replayed += 1
            except Exception:
                failed += 1
                quarantine_lines.append(line)
            next_line_index = index + 1
    finally:
        remaining_lines = [line for line in lines[next_line_index:] if line.strip()]
        _append_audit_quarantine(path, quarantine_lines + remaining_lines)
        snapshot_path.unlink(missing_ok=True)

    return AuditReplayResponse(replayed=replayed, failed=failed)
