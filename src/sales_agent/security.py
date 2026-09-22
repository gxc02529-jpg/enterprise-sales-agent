from __future__ import annotations

from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from sales_agent.config import Settings, get_settings
from sales_agent.contracts import Principal

bearer = HTTPBearer(auto_error=False)


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return value.split()
    return []


async def get_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Principal:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    token = credentials.credentials
    if settings.allow_dev_token and token in {"dev-token", "dev-admin-token"}:
        principal = Principal(
            user_id="demo-sales-001",
            tenant_id="demo-tenant",
            roles=["admin"] if token == "dev-admin-token" else ["sales"],
            scope_tags=["sales:demo-sales-001"],
        )
        request.state.principal = principal
        return principal
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            options={"require": ["exp", "sub", "tenant_id"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token"
        ) from exc
    scope_tags = _as_list(claims.get("scope_tags", claims.get("scope", [])))
    if not scope_tags and "admin" not in _as_list(claims.get("roles", [])):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="no data scope")
    principal = Principal(
        user_id=str(claims["sub"]),
        tenant_id=str(claims["tenant_id"]),
        roles=_as_list(claims.get("roles", [])),
        scope_tags=scope_tags,
    )
    request.state.principal = principal
    return principal


async def require_admin(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    if "admin" not in principal.roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    return principal
