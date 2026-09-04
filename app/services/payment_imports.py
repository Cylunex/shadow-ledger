"""Conservative text adapters; only explicitly recognized headers are accepted."""

import csv
import io
import re
from decimal import Decimal, InvalidOperation

from app.importers import (
    ImportCandidate,
    ParsedImport,
    _fingerprint,
    _markdown_table,
    _payload,
    _record,
    _time,
)
from app.schemas import ConsumptionInput

HEADERS = {
    "alipay": {"交易时间", "交易对方", "商品说明", "收/支", "金额", "交易状态", "交易订单号"},
    "wechat": {
        "交易时间",
        "交易类型",
        "交易对方",
        "商品",
        "收/支",
        "金额(元)",
        "当前状态",
        "交易单号",
    },
}


def _header(value: str) -> str:
    return value.strip().removeprefix("\ufeff").replace("（", "(").replace("）", ")")


def _table(content: str, format: str):
    if format == "markdown":
        headers, rows = _markdown_table(content)
        normalized = [_header(value) for value in headers]
        return normalized, [{_header(key): value for key, value in row.items()} for row in rows]
    reader = csv.reader(io.StringIO(content.removeprefix("\ufeff")), strict=True)
    headers = None
    rows = []
    for values in reader:
        if headers is None:
            candidate = [_header(value) for value in values]
            if any(required <= set(candidate) for required in HEADERS.values()):
                if len(set(candidate)) != len(candidate):
                    raise ValueError("账单表头存在重复列")
                headers = candidate
            elif reader.line_num > 100:
                break
            continue
        if not values or not any(value.strip() for value in values):
            continue
        if len(values) != len(headers):
            # Export footers are explicitly marked, never interpreted as transactions.
            if values[0].strip().startswith(("----------------", "导出时间", "共", "说明：")):
                break
            raise ValueError(f"第 {reader.line_num} 行列数不一致")
        rows.append({**dict(zip(headers, values, strict=True)), "_source_line": reader.line_num})
        if len(rows) > 1000:
            raise ValueError("单次导入最多 1000 行")
    if headers is None:
        raise ValueError("不支持此账单表头；仅接受已识别的支付宝／微信文本列")
    return headers, rows


def parse_payment_import(content: str, format: str) -> ParsedImport:
    try:
        headers, rows = _table(content, format)
    except csv.Error as exc:
        raise ValueError("CSV 引号或分隔符语法无效；整批未提交") from exc
    if len(set(headers)) != len(headers) or len(rows) > 1000:
        raise ValueError("表头重复或单次超过 1000 行")
    platform = next((name for name, required in HEADERS.items() if required <= set(headers)), None)
    if not platform:
        raise ValueError("不支持此支付账单表头")
    candidates = []
    skipped = 0
    warnings = []
    for row in rows:
        line = row["_source_line"]
        status = row.get("交易状态", row.get("当前状态", "")).strip()
        direction = row["收/支"].strip()
        trade_type = row.get("交易类型", "").strip()
        # A refunded expense remains its original expense. Only an explicit incoming
        # refund is a refund fact; we never synthesize one from a status label.
        if any(word in trade_type for word in ("转账", "红包", "充值", "提现", "理财", "还款")):
            skipped += 1
            continue
        if direction not in {"收入", "支出"} or any(
            word in status for word in ("关闭", "失败", "待支付", "未支付")
        ):
            skipped += 1
            continue
        if not any(word in status for word in ("成功", "支付", "退款", "已收钱", "已完成")):
            skipped += 1
            warnings.append(f"第 {line} 行状态未识别，未生成草稿")
            continue
        amount_raw = row.get("金额", row.get("金额(元)", "")).strip()
        cleaned = re.sub(r"^[¥￥]", "", amount_raw).replace(",", "").strip()
        try:
            if not re.fullmatch(r"\d+(?:\.\d{1,4})?", cleaned):
                raise ValueError()
            amount = Decimal(cleaned)
            if amount <= 0:
                raise ValueError()
            occurred_at = _time(row["交易时间"])
        except (ValueError, InvalidOperation) as exc:
            raise ValueError(f"第 {line} 行金额或时间格式无效；整批未提交") from exc
        title = row.get("商品说明", row.get("商品", "")).strip()
        merchant = row["交易对方"].strip()
        money_type = (
            "refund"
            if direction == "收入" and "退款" in (trade_type + status + title)
            else ("income" if direction == "收入" else "expense")
        )
        external_id = row.get("交易订单号", row.get("交易单号", "")).strip().lstrip("'\t")
        if not external_id or external_id in {"-", "/"}:
            raise ValueError(f"第 {line} 行缺少交易标识，不能安全去重")
        consumption = (
            None
            if money_type != "expense"
            else ConsumptionInput(
                scene="other",
                merchant_name_raw=merchant or None,
                channel_name_raw="支付宝" if platform == "alipay" else "微信支付",
                lines=[],
            )
        )
        record = _record(
            occurred_at,
            money_type,
            amount,
            "other",
            title or merchant or "支付记录",
            consumption,
            "支付宝" if platform == "alipay" else "微信支付",
        )
        payload = {**_payload(platform, [row]), "format": format}
        candidates.append(ImportCandidate(record, _fingerprint(platform, external_id), payload))
    return ParsedImport(
        platform,
        len(rows),
        tuple(headers),
        tuple(candidates),
        skipped,
        tuple(
            warnings
            + ["转账、红包、充值、提现、理财、还款和非收支条目不生成消费事实；所有候选均需确认。"]
        ),
    )


def parse_text_import(content: str, format: str) -> ParsedImport:
    from app.importers import parse_markdown_import

    if format == "markdown":
        headers, _ = _markdown_table(content)
        if any(required <= {_header(value) for value in headers} for required in HEADERS.values()):
            return parse_payment_import(content, format)
        return parse_markdown_import(content)
    return parse_payment_import(content, format)
