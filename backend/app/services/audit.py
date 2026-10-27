"""安全审计：记录用户关键操作，供 /api/audit-logs 查询。

只负责添加记录；由调用方在合适的时机 commit，避免中途打断事务。
"""
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AuditLog


def audit(
    db: Session,
    user_id: int | None,
    action: str,
    entity_type: str = "",
    entity_id: int | None = None,
    detail: str = "",
    ip: str | None = None,
) -> None:
    if not settings.audit_enabled:
        return
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=str(detail)[:2000],
            ip=ip or "",
        )
    )
