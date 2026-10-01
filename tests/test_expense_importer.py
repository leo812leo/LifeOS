"""tests/test_expense_importer.py — expense_importer.py 的單元測試。"""

import csv
from pathlib import Path
from typing import List

import pytest

from scripts.expense_importer import (
    ExpenseRecord,
    detect_source,
    normalize_account,
    normalize_category,
    parse_cwmoney_csv,
    parse_moneybook_csv,
    _normalize_date,
)


# ── normalize_category ──────────────────────────────────────────────────


class TestNormalizeCategory:
    """類別映射測試。"""

    def test_food_categories_map_to_dining(self) -> None:
        assert normalize_category("食物") == "餐飲"
        assert normalize_category("早餐") == "餐飲"
        assert normalize_category("Food") == "餐飲"
        assert normalize_category("外食") == "餐飲"

    def test_transport_categories(self) -> None:
        assert normalize_category("交通") == "交通"
        assert normalize_category("油費") == "交通"
        assert normalize_category("捷運") == "交通"

    def test_unknown_category_falls_to_other(self) -> None:
        assert normalize_category("不存在的分類") == "其他"

    def test_empty_string_returns_other(self) -> None:
        assert normalize_category("") == "其他"

    def test_whitespace_stripped(self) -> None:
        assert normalize_category("  食物  ") == "餐飲"


# ── normalize_account ───────────────────────────────────────────────────


class TestNormalizeAccount:
    """帳戶映射測試。"""

    def test_cash_aliases(self) -> None:
        assert normalize_account("現金") == "現金"
        assert normalize_account("Cash") == "現金"

    def test_bank_aliases(self) -> None:
        assert normalize_account("永豐銀行") == "永豐"
        assert normalize_account("國泰世華") == "國泰"
        assert normalize_account("中國信託") == "中信"

    def test_unknown_account_falls_to_other(self) -> None:
        assert normalize_account("不存在的銀行") == "其他"


# ── _normalize_date ─────────────────────────────────────────────────────


class TestNormalizeDate:
    """日期格式正規化測試。"""

    def test_iso_format_unchanged(self) -> None:
        assert _normalize_date("2026-04-15") == "2026-04-15"

    def test_slash_format_padded(self) -> None:
        assert _normalize_date("2026/4/15") == "2026-04-15"
        assert _normalize_date("2026/12/31") == "2026-12-31"

    def test_strips_time_component(self) -> None:
        assert _normalize_date("2026-04-15 12:00:00") == "2026-04-15"
        assert _normalize_date("2026-04-15T08:30:00") == "2026-04-15"

    def test_empty_returns_empty(self) -> None:
        assert _normalize_date("") == ""


# ── parse_moneybook_csv ─────────────────────────────────────────────────


class TestParseMoneybookCsv:
    """麻布記帳 CSV 解析測試。"""

    def _write_csv(self, tmp_path: Path, rows: List[List[str]]) -> Path:
        filepath = tmp_path / "moneybook.csv"
        with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerows(rows)
        return filepath

    def test_parses_basic_expense(self, tmp_path: Path) -> None:
        filepath = self._write_csv(tmp_path, [
            ["日期", "金額", "分類", "帳戶", "描述"],
            ["2026/4/15", "120", "食物", "信用卡", "午餐便當"],
        ])
        records = parse_moneybook_csv(str(filepath))
        assert len(records) == 1
        assert records[0].date == "2026-04-15"
        assert records[0].amount == 120.0
        assert records[0].category == "餐飲"
        assert records[0].account == "信用卡"
        assert records[0].notes == "午餐便當"
        assert records[0].source == "麻布記帳"
        assert records[0].type == "支出"

    def test_negative_amount_treated_as_income(self, tmp_path: Path) -> None:
        filepath = self._write_csv(tmp_path, [
            ["日期", "金額", "分類", "帳戶", "描述"],
            ["2026/4/15", "-50000", "其他", "永豐", "薪水"],
        ])
        records = parse_moneybook_csv(str(filepath))
        assert records[0].type == "收入"
        assert records[0].amount == 50000.0  # 內部統一存正數

    def test_skips_zero_amount(self, tmp_path: Path) -> None:
        filepath = self._write_csv(tmp_path, [
            ["日期", "金額", "分類", "帳戶", "描述"],
            ["2026/4/15", "0", "其他", "現金", ""],
            ["2026/4/16", "100", "食物", "現金", ""],
        ])
        records = parse_moneybook_csv(str(filepath))
        assert len(records) == 1

    def test_skips_invalid_rows(self, tmp_path: Path) -> None:
        filepath = self._write_csv(tmp_path, [
            ["日期", "金額", "分類", "帳戶", "描述"],
            ["", "abc", "", "", ""],  # 無效金額
            ["2026/4/15", "100", "食物", "現金", ""],
        ])
        records = parse_moneybook_csv(str(filepath))
        assert len(records) == 1


# ── parse_cwmoney_csv ───────────────────────────────────────────────────


class TestParseCwmoneyCsv:
    """CWMoney CSV 解析測試。"""

    def _write_csv(self, tmp_path: Path, rows: List[List[str]]) -> Path:
        filepath = tmp_path / "cwmoney.csv"
        with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerows(rows)
        return filepath

    def test_parses_chinese_headers(self, tmp_path: Path) -> None:
        filepath = self._write_csv(tmp_path, [
            ["Id", "日期", "金額", "分類", "帳號", "備註"],
            ["1", "2026-04-15", "350", "交通", "信用卡", "高鐵車票"],
        ])
        records = parse_cwmoney_csv(str(filepath))
        assert len(records) == 1
        assert records[0].category == "交通"
        assert records[0].source == "CWMoney"

    def test_parses_english_headers(self, tmp_path: Path) -> None:
        filepath = self._write_csv(tmp_path, [
            ["Id", "Date", "Amount", "Category", "Account", "Notes"],
            ["1", "2026-04-15", "200", "Food", "Cash", "lunch"],
        ])
        records = parse_cwmoney_csv(str(filepath))
        assert len(records) == 1
        assert records[0].category == "餐飲"
        assert records[0].account == "現金"


# ── detect_source ───────────────────────────────────────────────────────


class TestDetectSource:
    """來源偵測測試。"""

    def test_detects_moneybook_from_chinese_headers(self, tmp_path: Path) -> None:
        filepath = tmp_path / "test.csv"
        with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["日期", "金額", "分類", "帳戶"])
        assert detect_source(str(filepath)) == "moneybook"

    def test_detects_cwmoney_from_english_headers(self, tmp_path: Path) -> None:
        filepath = tmp_path / "test.csv"
        with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Id", "Date", "Amount", "Category", "Subcategory"])
        assert detect_source(str(filepath)) == "cwmoney"

    def test_returns_unknown_for_arbitrary_csv(self, tmp_path: Path) -> None:
        filepath = tmp_path / "test.csv"
        with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["foo", "bar", "baz"])
        assert detect_source(str(filepath)) == "unknown"
