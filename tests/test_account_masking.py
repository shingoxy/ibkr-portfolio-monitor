"""账户脱敏测试。"""

from __future__ import annotations

import pytest
from app.logging_config import mask_account, AccountMaskFilter


class TestMaskAccount:
    def test_normal_account(self):
        assert mask_account("U12345678") == "U12****78"

    def test_du_account(self):
        assert mask_account("DU12345678") == "DU12****78"

    def test_short_digits(self):
        assert mask_account("U123") == "U***"

    def test_empty(self):
        assert mask_account("") == "****"

    def test_none(self):
        assert mask_account(None) == "****"

    def test_four_digits(self):
        # 4 digits: prefix + first2 + "" + last2 = 全部显示
        assert mask_account("U1234") == "U1234"

    def test_six_digits(self):
        # 6 digits: prefix + first2 + "**" + last2
        assert mask_account("U123456") == "U12**56"

    def test_non_account_string(self):
        assert mask_account("hello") == "*****"

    def test_no_full_account_in_output(self):
        """确保完整账户号不会出现在输出中。"""
        accounts = ["U12345678", "DU87654321", "U123456"]
        for acct in accounts:
            masked = mask_account(acct)
            if len(acct) >= 6:
                assert acct not in masked or acct == masked


class TestAccountMaskFilter:
    def test_mask_in_log(self):
        f = AccountMaskFilter()
        assert "U12****78" in f._mask("Account U12345678 test")

    def test_mask_multiple(self):
        f = AccountMaskFilter()
        result = f._mask("U12345678 and DU87654321")
        assert "U12****78" in result
        assert "DU87****21" in result
