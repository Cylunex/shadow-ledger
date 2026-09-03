"""Add optional, bounded payment method labels without rewriting historical facts."""

import sqlalchemy as sa
from alembic import op

revision = "20260903_0006"
down_revision = "20260831_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("money_entries", sa.Column("payment_method", sa.String(24), nullable=True))
    op.create_check_constraint(
        "ck_money_payment_method",
        "money_entries",
        "payment_method IS NULL OR payment_method IN "
        "('alipay','wechat','jd_pay','jd_baitiao','huabei','gift_card','cash',"
        "'bank_card','bank_transfer','mixed','other')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_money_payment_method", "money_entries", type_="check")
    op.drop_column("money_entries", "payment_method")
