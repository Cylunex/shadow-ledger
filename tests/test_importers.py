from __future__ import annotations

from decimal import Decimal

from app.importers import parse_markdown_import


def table(headers: list[str], rows: list[list[str]]) -> str:
    return "\n".join(
        [
            f"| {' | '.join(headers)} |",
            f"| {' | '.join('---' for _ in headers)} |",
            *(f"| {' | '.join(row)} |" for row in rows),
        ]
    )


def test_jd_import_keeps_refund_separate_and_ignores_payment_domain():
    content = table(
        [
            "交易时间",
            "商户名称",
            "交易说明",
            "金额",
            "收/付款方式",
            "交易状态",
            "收/支",
            "交易分类",
            "交易订单号",
            "商家订单号",
            "备注",
        ],
        [
            [
                "2026-08-01 12:00:00",
                "示例商户",
                "示例商品",
                "88.00(已全额退款)",
                "示例支付渠道",
                "交易成功",
                "不计收支",
                "食品酒饮",
                "T-1",
                "M-1",
                "",
            ],
            [
                "2026-08-02 12:00:00",
                "示例商户",
                "退款",
                "88.00",
                "示例支付渠道",
                "退款成功",
                "不计收支",
                "食品酒饮",
                "T-2",
                "M-2",
                "",
            ],
        ],
    )

    parsed = parse_markdown_import(content)

    assert parsed.platform == "jd"
    assert [item.record.money_entry.type for item in parsed.candidates] == ["expense", "refund"]
    assert parsed.candidates[0].record.money_entry.amount == 88
    assert parsed.candidates[0].record.money_entry.category_key == "food"
    assert parsed.candidates[1].record.consumption is None
    assert parsed.candidates[0].source_external_id != parsed.candidates[1].source_external_id
    assert "收/付款方式" not in parsed.candidates[0].record.model_dump_json()
    assert parsed.candidates[0].raw_payload["rows"][0]["收/付款方式"] == "示例支付渠道"


def test_taobao_import_groups_continuation_lines_and_uses_paid_total():
    content = table(
        [
            "订单号",
            "订单提交时间",
            "订单状态",
            "店铺名称",
            "商品名称",
            "商品链接",
            "型号款式",
            "商品数量",
            "商品金额",
            "实付金额",
            "运费",
        ],
        [
            [
                "ORDER-1",
                "2026-08-03 09:30:00",
                "交易成功",
                "示例店铺",
                "商品甲",
                "https://example.invalid/a",
                "红色",
                "1",
                "12.00",
                "20.00",
                "0.00",
            ],
            ["", "", "", "", "商品乙", "https://example.invalid/b", "", "2", "15.00", "", ""],
            [
                "ORDER-2",
                "2026-08-04 09:30:00",
                "交易关闭",
                "示例店铺",
                "商品丙",
                "",
                "",
                "1",
                "9.00",
                "9.00",
                "0.00",
            ],
        ],
    )

    parsed = parse_markdown_import(content)

    assert parsed.platform == "taobao"
    assert len(parsed.candidates) == 1
    assert parsed.skipped_count == 1
    record = parsed.candidates[0].record
    assert record.money_entry.amount == 20
    assert [line.raw_name for line in record.consumption.lines] == ["商品甲", "商品乙"]
    assert [line.amount for line in record.consumption.lines] == [12, 15]
    assert record.consumption.lines[1].quantity == 2


def test_meituan_import_does_not_invent_refund_relation_or_scene():
    content = table(
        [
            "交易创建时间",
            "交易成功时间",
            "交易类型",
            "订单标题",
            "收/支",
            "支付方式",
            "订单金额",
            "实付金额",
            "交易单号",
            "商家单号",
            "备注",
        ],
        [
            [
                "2026-08-05 10:00:00",
                "2026-08-05 10:01:00",
                "支付",
                "示例订单",
                "支出",
                "示例支付渠道",
                "30.00",
                "28.00",
                "PAY-1",
                "MERCHANT-1",
                "",
            ],
            [
                "2026-08-06 10:00:00",
                "2026-08-06 10:01:00",
                "退款",
                "示例退款",
                "收入",
                "",
                "5.00",
                "5.00",
                "REFUND-1",
                "MERCHANT-2",
                "",
            ],
        ],
    )

    parsed = parse_markdown_import(content)

    assert parsed.candidates[0].record.consumption.scene == "other"
    refund = parsed.candidates[1]
    assert refund.record.money_entry.type == "refund"
    assert refund.record.money_entry.related_entry_id is None
    assert refund.record.consumption is None
    assert refund.warnings == ("refund_unlinked",)


def test_eleme_import_parses_product_quantities():
    content = table(
        [
            "下单时间",
            "订单号",
            "商户信息",
            "商品及数量",
            "商品描述",
            "订单金额(元)",
            "订单状态",
            "订单子类型",
        ],
        [
            [
                "2026-08-07 18:30:00",
                "E-1",
                "示例餐厅",
                "1、商品:主食, 数量:1;<br>2、商品:饮料, 数量:2;",
                "",
                "42.50",
                "已完成",
                "",
            ]
        ],
    )

    parsed = parse_markdown_import(content)

    record = parsed.candidates[0].record
    assert record.money_entry.amount == Decimal("42.50")
    assert record.money_entry.category_key == "food"
    assert record.consumption.scene == "delivery"
    assert [line.raw_name for line in record.consumption.lines] == ["主食", "饮料"]
    assert [line.quantity for line in record.consumption.lines] == [1, 2]


def test_markdown_preview_and_commit_are_idempotent(client, write_headers):
    content = table(
        [
            "下单时间",
            "订单号",
            "商户信息",
            "商品及数量",
            "商品描述",
            "订单金额(元)",
            "订单状态",
            "订单子类型",
        ],
        [
            [
                "2026-08-08 12:00:00",
                "E-API-1",
                "示例餐厅",
                "1、商品:午餐, 数量:1;",
                "",
                "25.00",
                "已完成",
                "",
            ]
        ],
    )
    body = {"format": "markdown", "content": content}

    preview = client.post("/api/v1/imports/preview", json=body, headers=write_headers)
    assert preview.status_code == 200, preview.text
    assert preview.json()["record_count"] == 1
    assert preview.json()["items"][0]["record"]["confirm"] is False

    first = client.post(
        "/api/v1/imports/commit",
        json={"source": body},
        headers={**write_headers, "Idempotency-Key": "bill-import-1"},
    )
    assert first.status_code == 201, first.text
    assert first.json()["created_count"] == 1

    replay = client.post(
        "/api/v1/imports/commit",
        json={"source": body},
        headers={**write_headers, "Idempotency-Key": "bill-import-2"},
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["created_count"] == 0
    assert replay.json()["duplicate_count"] == 1
    assert replay.json()["record_ids"] == first.json()["record_ids"]

    drafts = client.get(
        "/api/v1/records?state=draft", headers={"X-Dev-User": "alice"}
    ).json()["items"]
    assert len(drafts) == 1
    assert drafts[0]["money_entry"]["amount"] == "25.0000"
