from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from app.schemas import ConsumptionInput, ConsumptionLineInput, MoneyEntryInput, RecordCreate

TIMEZONE = ZoneInfo("Asia/Shanghai")
PARSER_VERSION = "1"

JD_HEADERS = {
    "交易时间",
    "商户名称",
    "交易说明",
    "金额",
    "交易状态",
    "收/支",
    "交易分类",
    "交易订单号",
    "商家订单号",
}
TAOBAO_HEADERS = {
    "订单号",
    "订单提交时间",
    "订单状态",
    "店铺名称",
    "商品名称",
    "商品数量",
    "商品金额",
    "实付金额",
}
MEITUAN_HEADERS = {
    "交易创建时间",
    "交易成功时间",
    "交易类型",
    "订单标题",
    "收/支",
    "订单金额",
    "实付金额",
    "交易单号",
}
ELEME_HEADERS = {
    "下单时间",
    "订单号",
    "商户信息",
    "商品及数量",
    "订单金额(元)",
    "订单状态",
}


@dataclass(frozen=True)
class ImportCandidate:
    record: RecordCreate
    source_external_id: str
    raw_payload: dict[str, Any]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedImport:
    platform: str
    row_count: int
    columns: tuple[str, ...]
    candidates: tuple[ImportCandidate, ...]
    skipped_count: int
    warnings: tuple[str, ...]


def _cells(line: str) -> list[str]:
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    return [cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", value)]


def _markdown_table(content: str) -> tuple[list[str], list[dict[str, str]]]:
    lines = content.removeprefix("\ufeff").splitlines()
    for index in range(len(lines) - 1):
        if not lines[index].lstrip().startswith("|"):
            continue
        headers = _cells(lines[index])
        separators = _cells(lines[index + 1])
        if len(headers) != len(separators) or not all(
            re.fullmatch(r":?-{3,}:?", item.replace(" ", "")) for item in separators
        ):
            continue
        rows: list[dict[str, str]] = []
        for line in lines[index + 2 :]:
            if not line.strip():
                if rows:
                    break
                continue
            if not line.lstrip().startswith("|"):
                break
            values = _cells(line)
            if len(values) != len(headers):
                raise ValueError("Markdown 表格存在列数不一致的行")
            rows.append(dict(zip(headers, values, strict=True)))
        return headers, rows
    raise ValueError("没有找到 Markdown 表格")


def _clip(value: str | None, limit: int) -> str:
    return (value or "").strip()[:limit]


def _money(value: str) -> Decimal:
    match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", value.replace("，", ","))
    if not match:
        raise ValueError("金额缺失")
    try:
        amount = Decimal(match.group(0).replace(",", "")).copy_abs()
    except InvalidOperation as exc:
        raise ValueError("金额格式无效") from exc
    if amount <= 0:
        raise ValueError("金额必须大于零")
    return amount


def _quantity(value: str) -> Decimal | None:
    match = re.search(r"\d+(?:\.\d+)?", value)
    if not match:
        return None
    result = Decimal(match.group(0))
    return result if result > 0 else None


def _time(value: str) -> datetime:
    normalized = value.strip().replace("/", "-")
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, pattern).replace(tzinfo=TIMEZONE)
        except ValueError:
            pass
    raise ValueError("日期格式无效")


def _fingerprint(platform: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{platform}:{digest}"


def _payload(platform: str, rows: list[dict[str, str]], warnings: tuple[str, ...] = ()):
    return {"platform": platform, "rows": rows, "warnings": list(warnings)}


def _record(
    occurred_at: datetime,
    money_type: str,
    amount: Decimal,
    category: str,
    title: str,
    consumption: ConsumptionInput | None,
) -> RecordCreate:
    return RecordCreate(
        occurred_at=occurred_at,
        timezone="Asia/Shanghai",
        note="",
        money_entry=MoneyEntryInput(
            type=money_type,
            amount=amount,
            currency="CNY",
            category_key=category,
            title=_clip(title, 500),
        ),
        consumption=consumption,
        confirm=False,
    )


def _jd_category(value: str) -> str:
    mappings = {
        "food": ("食品", "酒饮"),
        "entertainment": ("休闲娱乐",),
        "health": ("医疗", "保健"),
        "shopping": (
            "网购",
            "美妆",
            "个护",
            "钟表",
            "眼镜",
            "运动",
            "户外",
            "服饰",
            "内衣",
            "清洁",
            "纸品",
            "数码",
            "电器",
            "手机",
            "通讯",
            "电脑",
            "办公",
        ),
    }
    for category, needles in mappings.items():
        if any(needle in value for needle in needles):
            return category
    return "other"


def _parse_jd(headers: list[str], rows: list[dict[str, str]]) -> ParsedImport:
    candidates: list[ImportCandidate] = []
    skipped = 0
    excluded = 0
    refunds = 0
    for row in rows:
        status = row["交易状态"].strip()
        if status not in {"交易成功", "退款成功"}:
            skipped += 1
            continue
        money_type = "refund" if status == "退款成功" else "expense"
        warnings: list[str] = []
        if row.get("收/支", "").strip() == "不计收支" and money_type == "expense":
            warnings.append("platform_excluded_from_balance")
            excluded += 1
        if money_type == "refund":
            warnings.append("refund_unlinked")
            refunds += 1
        consumption = None
        if money_type == "expense":
            description = _clip(row.get("交易说明"), 1000) or "京东订单"
            consumption = ConsumptionInput(
                scene="online_purchase",
                merchant_name_raw=_clip(row.get("商户名称"), 1000) or None,
                channel_name_raw="京东",
                lines=[ConsumptionLineInput(raw_name=description, sort_order=0)],
            )
        warning_tuple = tuple(warnings)
        identity = (
            row.get("交易订单号", ""),
            row.get("商家订单号", ""),
            status,
            row.get("交易时间", ""),
            row.get("金额", ""),
        )
        candidates.append(
            ImportCandidate(
                record=_record(
                    _time(row["交易时间"]),
                    money_type,
                    _money(row["金额"]),
                    _jd_category(row.get("交易分类", "")),
                    row.get("交易说明", "") or row.get("商户名称", "") or "京东交易",
                    consumption,
                ),
                source_external_id=_fingerprint("jd", *identity),
                raw_payload=_payload("jd", [row], warning_tuple),
                warnings=warning_tuple,
            )
        )
    warnings = ["支付方式仅保留在来源数据中，不进入账本领域模型"]
    if excluded:
        warnings.append(f"{excluded} 条平台标记为不计收支，仍作为可核对的消费草稿导入")
    if refunds:
        warnings.append(f"{refunds} 条退款缺少可靠原单关联，保持为独立退款草稿")
    if skipped:
        warnings.append(f"跳过 {skipped} 条非成功状态记录")
    return ParsedImport(
        "jd", len(rows), tuple(headers), tuple(candidates), skipped, tuple(warnings)
    )


def _taobao_groups(rows: list[dict[str, str]]) -> tuple[list[list[dict[str, str]]], int]:
    groups: list[list[dict[str, str]]] = []
    skipped = 0
    current: list[dict[str, str]] | None = None
    for row in rows:
        if row.get("订单号", "").strip():
            current = [row]
            groups.append(current)
        elif current is not None:
            current.append(row)
        else:
            skipped += 1
    return groups, skipped


def _taobao_line(row: dict[str, str], index: int):
    name = _clip(row.get("商品名称"), 1000) or "淘宝商品"
    notes: list[str] = []
    if row.get("型号款式", "").strip():
        notes.append(f"型号款式：{row['型号款式'].strip()}")
    if row.get("商品链接", "").strip():
        notes.append(f"商品链接：{row['商品链接'].strip()}")
    amount = None
    if row.get("商品金额", "").strip():
        amount = _money(row["商品金额"])
    return ConsumptionLineInput(
        raw_name=name,
        quantity=_quantity(row.get("商品数量", "")),
        amount=amount,
        note=_clip("；".join(notes), 2000),
        sort_order=index,
    )


def _parse_taobao(headers: list[str], rows: list[dict[str, str]]) -> ParsedImport:
    groups, skipped = _taobao_groups(rows)
    candidates: list[ImportCandidate] = []
    closed = 0
    for group in groups:
        order = group[0]
        status = order["订单状态"].strip()
        if status not in {"交易成功", "充值成功"}:
            skipped += 1
            if status == "交易关闭":
                closed += 1
            continue
        lines = [_taobao_line(row, index) for index, row in enumerate(group)]
        title = lines[0].raw_name
        if len(lines) > 1:
            title = f"{title} 等 {len(lines)} 项商品"
        consumption = ConsumptionInput(
            scene="online_purchase",
            merchant_name_raw=_clip(order.get("店铺名称"), 1000) or None,
            channel_name_raw="淘宝",
            lines=lines,
        )
        candidates.append(
            ImportCandidate(
                record=_record(
                    _time(order["订单提交时间"]),
                    "expense",
                    _money(order["实付金额"]),
                    "services" if status == "充值成功" else "shopping",
                    title,
                    consumption,
                ),
                source_external_id=_fingerprint(
                    "taobao", order.get("订单号", "") or json.dumps(group, sort_keys=True)
                ),
                raw_payload=_payload("taobao", group),
            )
        )
    warnings = ["订单实付金额作为最终金额，商品金额只保留为消费明细"]
    if closed:
        warnings.append(f"跳过 {closed} 个交易关闭订单")
    if skipped - closed:
        warnings.append(f"跳过 {skipped - closed} 条无法归组或非成功状态数据")
    return ParsedImport(
        "taobao", len(rows), tuple(headers), tuple(candidates), skipped, tuple(warnings)
    )


def _parse_meituan(headers: list[str], rows: list[dict[str, str]]) -> ParsedImport:
    candidates: list[ImportCandidate] = []
    skipped = 0
    refunds = 0
    for row in rows:
        trade_type = row["交易类型"].strip()
        direction = row.get("收/支", "").strip()
        if "退款" in trade_type or direction == "收入":
            money_type = "refund"
            refunds += 1
        elif "支付" in trade_type or direction == "支出":
            money_type = "expense"
        else:
            skipped += 1
            continue
        title = row.get("订单标题", "") or "美团交易"
        warnings = ("refund_unlinked",) if money_type == "refund" else ()
        consumption = None
        if money_type == "expense":
            consumption = ConsumptionInput(
                scene="other",
                channel_name_raw="美团",
                lines=[ConsumptionLineInput(raw_name=_clip(title, 1000), sort_order=0)],
            )
        occurred = row.get("交易成功时间", "") or row.get("交易创建时间", "")
        candidates.append(
            ImportCandidate(
                record=_record(
                    _time(occurred),
                    money_type,
                    _money(row.get("实付金额", "") or row.get("订单金额", "")),
                    "other",
                    title,
                    consumption,
                ),
                source_external_id=_fingerprint(
                    "meituan",
                    row.get("交易单号", "") or json.dumps(row, sort_keys=True),
                    trade_type,
                ),
                raw_payload=_payload("meituan", [row], warnings),
                warnings=warnings,
            )
        )
    warnings = ["支付方式仅保留在来源数据中；无法可靠判断业务场景，消费场景暂记为其他"]
    if refunds:
        warnings.append(f"{refunds} 条退款缺少可靠原单关联，保持为独立退款草稿")
    if skipped:
        warnings.append(f"跳过 {skipped} 条无法识别收支方向的数据")
    return ParsedImport(
        "meituan", len(rows), tuple(headers), tuple(candidates), skipped, tuple(warnings)
    )


def _eleme_lines(value: str, description: str) -> list[ConsumptionLineInput]:
    chunks = re.split(r"(?:<br\s*/?>|\r?\n)+", value, flags=re.IGNORECASE)
    result: list[ConsumptionLineInput] = []
    for chunk in chunks:
        cleaned = re.sub(r"^\s*\d+\s*[、.)）]\s*", "", chunk).strip(" ;；")
        if not cleaned:
            continue
        match = re.search(
            r"商品\s*[:：]\s*(.*?)\s*[,，;；]\s*数量\s*[:：]\s*([\d.]+)", cleaned
        )
        name = match.group(1).strip() if match else cleaned
        quantity = _quantity(match.group(2)) if match else None
        result.append(
            ConsumptionLineInput(
                raw_name=_clip(name, 1000),
                quantity=quantity,
                note=_clip(description, 2000),
                sort_order=len(result),
            )
        )
    return result or [
        ConsumptionLineInput(
            raw_name=_clip(value, 1000) or "饿了么订单",
            note=_clip(description, 2000),
            sort_order=0,
        )
    ]


def _parse_eleme(headers: list[str], rows: list[dict[str, str]]) -> ParsedImport:
    candidates: list[ImportCandidate] = []
    skipped = 0
    for row in rows:
        if row["订单状态"].strip() != "已完成":
            skipped += 1
            continue
        lines = _eleme_lines(row.get("商品及数量", ""), row.get("商品描述", ""))
        title = lines[0].raw_name
        if len(lines) > 1:
            title = f"{title} 等 {len(lines)} 项商品"
        consumption = ConsumptionInput(
            scene="delivery",
            merchant_name_raw=_clip(row.get("商户信息"), 1000) or None,
            channel_name_raw="饿了么",
            note=_clip(row.get("订单子类型"), 2000),
            lines=lines,
        )
        candidates.append(
            ImportCandidate(
                record=_record(
                    _time(row["下单时间"]),
                    "expense",
                    _money(row["订单金额(元)"]),
                    "food",
                    title,
                    consumption,
                ),
                source_external_id=_fingerprint(
                    "eleme", row.get("订单号", "") or json.dumps(row, sort_keys=True)
                ),
                raw_payload=_payload("eleme", [row]),
            )
        )
    warnings = []
    if skipped:
        warnings.append(f"跳过 {skipped} 条非完成状态订单")
    return ParsedImport(
        "eleme", len(rows), tuple(headers), tuple(candidates), skipped, tuple(warnings)
    )


def parse_markdown_import(content: str) -> ParsedImport:
    headers, rows = _markdown_table(content)
    if len(rows) > 1000:
        raise ValueError("单次导入最多 1000 行")
    header_set = set(headers)
    if JD_HEADERS <= header_set:
        return _parse_jd(headers, rows)
    if TAOBAO_HEADERS <= header_set:
        return _parse_taobao(headers, rows)
    if MEITUAN_HEADERS <= header_set:
        return _parse_meituan(headers, rows)
    if ELEME_HEADERS <= header_set:
        return _parse_eleme(headers, rows)
    raise ValueError("暂不支持此 Markdown 账单表头")
