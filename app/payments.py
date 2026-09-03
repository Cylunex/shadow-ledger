"""Payment labels only: no accounts, balances, allocations or execution."""

from typing import Literal

PaymentMethod = Literal[
    "alipay",
    "wechat",
    "jd_pay",
    "jd_baitiao",
    "huabei",
    "gift_card",
    "cash",
    "bank_card",
    "bank_transfer",
    "mixed",
    "other",
]

PAYMENT_METHOD_LABELS = {
    "alipay": "支付宝",
    "wechat": "微信支付",
    "jd_pay": "京东支付",
    "jd_baitiao": "京东白条",
    "huabei": "花呗",
    "gift_card": "礼品卡",
    "cash": "现金",
    "bank_card": "银行卡",
    "bank_transfer": "银行转账",
    "mixed": "混合支付",
    "other": "其他",
}


def parse_payment_method(raw: str | None) -> PaymentMethod | None:
    """Conservative exact mapping; never infer a method from a merchant/platform."""
    value = (raw or "").strip()
    aliases = {label: key for key, label in PAYMENT_METHOD_LABELS.items()}
    aliases.update(
        {
            "微信": "wechat",
            "白条": "jd_baitiao",
            "京东礼品卡": "gift_card",
            "京东E卡": "gift_card",
            "礼品卡支付": "gift_card",
            "现金支付": "cash",
        }
    )
    return aliases.get(value, value if value in PAYMENT_METHOD_LABELS else None)
