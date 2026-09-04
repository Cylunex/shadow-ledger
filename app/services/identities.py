from sqlalchemy import select

from app.errors import AppError
from app.models import AuditEvent, ItemIdentity, Merchant
from app.services.records import idempotency_lookup, idempotency_save


def edit_identity(db, owner_id, kind, identity_id, data, key, *, alias=False):
    from app.models import ItemAlias, MerchantAlias
    from app.schemas import jsonable

    model = Merchant if kind == "merchant" else ItemIdentity
    operation = f"{kind}.alias" if alias else f"{kind}.edit"
    payload = {"id": str(identity_id), **jsonable(data.model_dump())}
    replay = idempotency_lookup(db, owner_id, operation, key, payload)
    alias_model = MerchantAlias if kind == "merchant" else ItemAlias
    if replay:
        return db.get(alias_model if alias else model, replay.resource_id)
    row = db.scalar(
        select(model)
        .where(model.id == identity_id, model.owner_id == owner_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise AppError(404, "identity_not_found", "身份不存在")
    if not row.active:
        raise AppError(409, "identity_inactive", "请编辑有效的规范身份")
    if alias:
        foreign_key = "merchant_id" if kind == "merchant" else "item_identity_id"
        normalized = "".join(data.alias.casefold().split())
        if not normalized:
            raise AppError(422, "empty_alias", "别名不能为空")
        found = db.scalar(
            select(alias_model).where(
                getattr(alias_model, foreign_key) == row.id,
                alias_model.normalized_alias == normalized,
            )
        )
        row = found or alias_model(
            **{
                foreign_key: row.id,
                "alias": data.alias,
                "normalized_alias": normalized,
                "source": data.source,
            }
        )
        db.add(row)
    else:
        merchant_id = getattr(data, "merchant_id", None)
        if merchant_id and not db.scalar(
            select(Merchant.id).where(
                Merchant.id == merchant_id, Merchant.owner_id == owner_id, Merchant.active.is_(True)
            )
        ):
            raise AppError(422, "merchant_not_found", "请选择本人有效商家")
        for name, value in data.model_dump().items():
            setattr(row, name, value)
    db.flush()
    db.add(
        AuditEvent(
            owner_id=owner_id,
            actor_type="user",
            actor_id=owner_id,
            action=operation,
            aggregate_type=kind,
            aggregate_id=identity_id,
        )
    )
    idempotency_save(db, owner_id, operation, key, payload, row.id)
    db.commit()
    return row


def merge_identity(db, owner_id, kind, source_id, target_id, reason, key, *, commit=True):
    model = Merchant if kind == "merchant" else ItemIdentity
    redirect = "canonical_merchant_id" if kind == "merchant" else "canonical_item_id"
    payload = {"source_id": str(source_id), "target_id": str(target_id), "reason": reason}
    operation = f"{kind}.merge"
    replay = idempotency_lookup(db, owner_id, operation, key, payload)
    if replay:
        return db.get(model, replay.resource_id)
    # Only active roots may merge. Ordered endpoint locks prevent A→B / B→A races.
    rows = {
        row.id: row
        for row in db.scalars(
            select(model)
            .where(model.owner_id == owner_id, model.id.in_([source_id, target_id]))
            .order_by(model.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    if source_id not in rows or target_id not in rows:
        raise AppError(404, "identity_not_found", "身份不存在")
    source, target = rows[source_id], rows[target_id]
    if source_id == target_id:
        raise AppError(422, "self_merge", "不能合并到自身")
    if (
        not source.active
        or not target.active
        or getattr(source, redirect)
        or getattr(target, redirect)
    ):
        raise AppError(409, "identity_already_merged", "请选择尚未合并且有效的身份")
    setattr(source, redirect, target.id)
    source.active = False
    db.add(
        AuditEvent(
            owner_id=owner_id,
            actor_type="user",
            actor_id=owner_id,
            action=f"{kind}.merged",
            aggregate_type=kind,
            aggregate_id=source.id,
            details={"target_id": str(target.id), "reason": reason},
        )
    )
    idempotency_save(db, owner_id, operation, key, payload, source.id)
    if commit:
        db.commit()
    else:
        db.flush()
    return source


def decide_suggestion(db, owner_id, suggestion_id, decision, key):
    from app.models import IdentitySuggestion

    payload = {"id": str(suggestion_id), "decision": decision}
    replay = idempotency_lookup(db, owner_id, "identity.decide", key, payload)
    if replay:
        return db.get(IdentitySuggestion, replay.resource_id)
    row = db.scalar(
        select(IdentitySuggestion)
        .where(IdentitySuggestion.owner_id == owner_id, IdentitySuggestion.id == suggestion_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise AppError(404, "suggestion_not_found", "身份建议不存在")
    if row.state != "pending":
        raise AppError(409, "suggestion_decided", "此建议已处理")
    if decision == "accept":
        if row.source_type not in {"merchant", "item"}:
            raise AppError(422, "unsupported_suggestion", "旧建议类型不支持直接合并，请手工整理")
        merge_identity(
            db,
            owner_id,
            row.source_type,
            row.source_id,
            row.target_id,
            "用户确认身份建议",
            f"suggestion:{row.id}",
            commit=False,
        )
        row.state = "accepted"
    else:
        row.state = "rejected"
    idempotency_save(db, owner_id, "identity.decide", key, payload, row.id)
    db.add(
        AuditEvent(
            owner_id=owner_id,
            actor_type="user",
            actor_id=owner_id,
            action="identity.suggestion.decided",
            aggregate_type="identity_suggestion",
            aggregate_id=row.id,
            details={"decision": decision},
        )
    )
    db.commit()
    return row


def propose_identities(db, owner_id):
    import hashlib
    from collections import defaultdict

    from app.models import IdentitySuggestion
    from app.services.records import lock_command

    lock_command(db, owner_id, "identity.propose", "scan")

    count = 0
    for kind, model in (("merchant", Merchant), ("item", ItemIdentity)):
        groups = defaultdict(list)
        rows = list(
            db.scalars(
                select(model)
                .where(model.owner_id == owner_id, model.active.is_(True))
                .order_by(model.id)
                .limit(500)
            )
        )
        for row in rows:
            name = "".join(row.canonical_name.casefold().split())
            signature = (
                name,
                getattr(row, "kind", None),
                getattr(row, "brand", None),
                getattr(row, "variant", None),
            )
            groups[signature].append(row)
        for group in groups.values():
            for source in group[1:]:
                target = group[0]
                key = hashlib.sha256(f"{kind}:{source.id}:{target.id}".encode()).hexdigest()
                if db.scalar(
                    select(IdentitySuggestion.id).where(
                        IdentitySuggestion.owner_id == owner_id,
                        IdentitySuggestion.suggestion_key == key,
                    )
                ):
                    continue
                db.add(
                    IdentitySuggestion(
                        owner_id=owner_id,
                        suggestion_key=key,
                        source_type=kind,
                        source_id=source.id,
                        target_id=target.id,
                        reason="规范名称及已提供规格相同；请人工核对，缺失规格不表示相同商品",
                    )
                )
                count += 1
    db.flush()
    return count


def family_ids(db, owner_id, kind, identity_id):
    model = Merchant if kind == "merchant" else ItemIdentity
    redirect = model.canonical_merchant_id if kind == "merchant" else model.canonical_item_id
    mapping = dict(db.execute(select(model.id, redirect).where(model.owner_id == owner_id)).all())
    if identity_id not in mapping:
        raise AppError(404, "identity_not_found", "身份不存在")

    def root(key):
        visited = set()
        while mapping.get(key):
            if key in visited:
                raise AppError(409, "identity_cycle", "历史身份链存在循环，需要修复")
            visited.add(key)
            key = mapping[key]
        return key

    canonical = root(identity_id)
    return canonical, [key for key in mapping if root(key) == canonical]
