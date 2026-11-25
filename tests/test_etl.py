#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""etl.py 的单元测试：只用标准库 unittest，`python -m unittest` 就能跑。"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import etl  # noqa: E402  （放在 sys.path 调整之后才能导入）


class ParseOrderRowTest(unittest.TestCase):
    """单行校验：每个失败分支都应该有明确的原因码。"""

    def row(self, **overrides):
        base = {
            "order_id": "A0001",
            "order_date": "2026-11-01",
            "region": "华东",
            "product": "键盘",
            "qty": "2",
            "unit_price": "199.00",
        }
        base.update(overrides)
        return base

    def test_good_row(self):
        order, reason = etl.parse_order_row(self.row())
        self.assertIsNone(reason)
        self.assertEqual(order.order_id, "A0001")
        self.assertEqual(order.amount, 398.0)

    def test_missing_order_id(self):
        order, reason = etl.parse_order_row(self.row(order_id=""))
        self.assertIsNone(order)
        self.assertEqual(reason, "missing_order_id")

    def test_impossible_date(self):
        # 格式对但日期不存在，strptime 会拦住（正则拦不住）
        order, reason = etl.parse_order_row(self.row(order_date="2026-02-30"))
        self.assertIsNone(order)
        self.assertEqual(reason, "bad_date")

    def test_qty_not_int(self):
        order, reason = etl.parse_order_row(self.row(qty="abc"))
        self.assertIsNone(order)
        self.assertEqual(reason, "qty_not_int")

    def test_qty_not_positive(self):
        order, reason = etl.parse_order_row(self.row(qty="-2"))
        self.assertIsNone(order)
        self.assertEqual(reason, "qty_not_positive")

    def test_price_nan(self):
        order, reason = etl.parse_order_row(self.row(unit_price="nan"))
        self.assertIsNone(order)
        self.assertEqual(reason, "price_invalid")

    def test_price_negative(self):
        order, reason = etl.parse_order_row(self.row(unit_price="-1"))
        self.assertIsNone(order)
        self.assertEqual(reason, "price_invalid")


class LoadCsvTest(unittest.TestCase):
    def test_duplicate_and_bad_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "orders.csv"
            csv_path.write_text(
                "order_id,order_date,region,product,qty,unit_price\n"
                "A1,2026-11-01,华东,键盘,1,100\n"
                "A1,2026-11-02,华东,键盘,1,100\n"  # 重复主键
                "A2,2026-13-01,华东,键盘,1,100\n",  # 月份非法
                encoding="utf-8",
            )
            quality = etl.Quality()
            orders = etl.load_orders_csv(csv_path, quality)

        self.assertEqual(len(orders), 1)
        self.assertEqual(quality.rejected, 2)
        self.assertEqual(quality.reasons["duplicate_order_id"], 1)
        self.assertEqual(quality.reasons["bad_date"], 1)


class ParseLogTest(unittest.TestCase):
    def test_mixed_log(self):
        text = (
            "2026-11-20T10:15:01+08:00 INFO  etl.start rows=16\n"
            "这不是日志行\n"
            "\n"
            "2026-11-20T10:15:06+08:00 ERROR etl.abort reason=disk_full path=/data/out\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "app.log"
            log_path.write_text(text, encoding="utf-8")
            stats = etl.parse_log(log_path)

        self.assertEqual(stats.lines, 4)
        self.assertEqual(stats.parsed, 2)
        self.assertEqual(stats.unparsed, 1)
        self.assertEqual(stats.blank, 1)
        self.assertEqual(stats.errors_total, 1)
        self.assertEqual(stats.errors[0]["event"], "etl.abort")
        self.assertEqual(stats.errors[0]["fields"]["reason"], "disk_full")


class WriteAtomicTest(unittest.TestCase):
    def test_writes_content_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "a.txt"
            etl.write_atomic(target, "hello\n")

            self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")
            leftovers = list(target.parent.glob("*.tmp"))
            self.assertEqual(leftovers, [])


class PipelineTest(unittest.TestCase):
    """最要紧的一条：连续跑两次，输出必须逐字节相同。"""

    def test_repeatable_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"

            etl.run(ROOT / "data", out_dir)
            first = {p.name: p.read_bytes() for p in out_dir.glob("*.*")}
            etl.run(ROOT / "data", out_dir)
            second = {p.name: p.read_bytes() for p in out_dir.glob("*.*")}

        self.assertEqual(sorted(first), ["rejected.csv", "summary.csv", "summary.json"])
        for name in first:
            self.assertEqual(first[name], second[name], "{} 两次运行不一致".format(name))

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            etl.run(ROOT / "data", out_dir, dry_run=True)
            self.assertFalse(out_dir.exists())

    def test_sample_data_quality(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = etl.run(ROOT / "data", Path(tmp) / "out")

        self.assertEqual(summary["quality"]["csv_lines"], 16)
        self.assertEqual(summary["quality"]["accepted"], 10)
        self.assertEqual(summary["totals"]["orders"], 10)
        self.assertEqual(summary["metadata"]["missing"], ["A1010"])
        self.assertEqual(summary["metadata"]["orphan"], ["A9999"])
        self.assertEqual(summary["logs"]["unparsed"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
