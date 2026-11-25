#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""订单数据 ETL：CSV + JSON + 日志  ->  三份可重复生成的报告。

为什么强调“可重复运行”（repeatable）
------------------------------------
1. 确定性：同样的输入必然产出字节相同的文件。做法是全部排序、不写运行时间戳。
2. 幂等：每次覆盖同一批文件，跑 100 次结果和跑 1 次一样，不会越跑越多。
3. 原子性：先写临时文件、fsync，再用 os.replace 换上去；中途崩溃不会留下半个文件。
4. 参数化：输入目录、输出目录、阈值都走命令行，不改代码就能复用。
5. 可测试：核心逻辑是纯函数，main() 只负责接线，单测可以随便调。

只依赖标准库，Python 3.8+ 可直接运行。
"""

import argparse
import csv
import io
import json
import logging
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1
DEFAULT_MAX_ERRORS = 20
# 给 CSV 报告加 BOM：Windows 上双击用 Excel 打开时才不会把中文显示成乱码
CSV_BOM = "\ufeff"
REQUIRED_CSV_COLUMNS = ("order_id", "order_date", "region", "product", "qty", "unit_price")

# 日志行：2026-11-20T10:15:01+08:00 INFO  etl.start rows=8
LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\s+"
    r"(?P<level>[A-Z]+)\s+(?P<event>\S+)(?P<rest>.*)$"
)
# 键值对：reason=disk_full  /  file="a b.json"
KV_RE = re.compile(r'([A-Za-z_][\w.]*)=("(?:[^"\\]|\\.)*"|\S+)')

logger = logging.getLogger("etl")


# --------------------------------------------------------------------------------------
# 数据模型：用 dataclass 而不是裸字典，字段拼错会立刻报错
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Order:
    order_id: str
    order_date: str
    region: str
    product: str
    qty: int
    unit_price: float

    @property
    def amount(self) -> float:
        return round(self.qty * self.unit_price, 2)


@dataclass(frozen=True)
class BadRow:
    source: str  # 哪个文件
    line: int  # 第几行 / 第几项
    reason: str  # 机器可读的原因
    raw: str  # 原始内容（截断），方便人工排查


@dataclass
class Quality:
    csv_lines: int = 0
    json_items: int = 0
    accepted: int = 0
    rejected: int = 0
    reasons: Dict[str, int] = field(default_factory=dict)
    bad_rows: List[BadRow] = field(default_factory=list)

    def reject(self, row: BadRow) -> None:
        self.rejected += 1
        self.bad_rows.append(row)
        self.reasons[row.reason] = self.reasons.get(row.reason, 0) + 1


@dataclass
class LogStats:
    lines: int = 0
    blank: int = 0
    parsed: int = 0
    unparsed: int = 0
    levels: Dict[str, int] = field(default_factory=dict)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    errors_total: int = 0


# --------------------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------------------
def write_atomic(path: Path, text: str) -> None:
    """原子写：同目录临时文件 -> 落盘 -> 替换。读者永远看不到写了一半的文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())  # 让操作系统真的把数据刷到磁盘
        os.replace(tmp_name, str(path))  # 同分区内替换是原子的
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------------------
def parse_order_row(row: Dict[str, str]) -> Tuple[Optional[Order], Optional[str]]:
    """校验并转换一行。成功返回 (Order, None)，失败返回 (None, 原因)。

    先检查、后转换，任何一步不合法就整行拒绝 —— 这是“不让脏数据进下游”的关键。
    """
    order_id = row.get("order_id", "")
    if not order_id:
        return None, "missing_order_id"

    # 用 strptime 而不是正则：能顺手挡掉 2026-02-30 这种“格式对但不存在”的日期
    try:
        datetime.strptime(row.get("order_date", ""), "%Y-%m-%d")
    except ValueError:
        return None, "bad_date"

    region = row.get("region", "")
    if not region:
        return None, "missing_region"

    product = row.get("product", "")
    if not product:
        return None, "missing_product"

    try:
        qty = int(row.get("qty", ""))
    except ValueError:
        return None, "qty_not_int"
    if qty <= 0:
        return None, "qty_not_positive"

    try:
        unit_price = float(row.get("unit_price", ""))
    except ValueError:
        return None, "price_not_number"
    if not math.isfinite(unit_price) or unit_price < 0:
        return None, "price_invalid"

    return Order(order_id, row["order_date"], region, product, qty, round(unit_price, 2)), None


def load_orders_csv(path: Path, quality: Quality) -> List[Order]:
    orders: List[Order] = []
    seen: Dict[str, int] = {}

    # utf-8-sig：顺手吃掉 Excel 另存为时加的 BOM；newline=""：交给 csv 模块处理换行
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = [(name or "").strip() for name in (reader.fieldnames or [])]
        missing = [c for c in REQUIRED_CSV_COLUMNS if c not in fieldnames]
        if missing:
            raise ValueError("{} 缺少必需列: {}".format(path.name, ", ".join(missing)))
        reader.fieldnames = fieldnames

        for raw_row in reader:
            line = reader.line_num
            # 每格 strip：Excel 里手工复制出来的数据经常带前后空格
            row = {k: (v or "").strip() for k, v in raw_row.items() if isinstance(k, str)}
            quality.csv_lines += 1

            if not any(row.values()):
                quality.reject(BadRow(path.name, line, "empty_row", ""))
                continue

            order, reason = parse_order_row(row)
            if order is None:
                raw = ",".join(row.get(c, "") for c in REQUIRED_CSV_COLUMNS)
                quality.reject(BadRow(path.name, line, reason or "unknown", raw))
                continue

            if order.order_id in seen:
                quality.reject(BadRow(path.name, line, "duplicate_order_id", order.order_id))
                continue

            seen[order.order_id] = line
            orders.append(order)

    quality.accepted = len(orders)
    return orders


# --------------------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------------------
def load_orders_json(path: Path, quality: Quality) -> Dict[str, Dict[str, Any]]:
    """把订单元数据读成 {order_id: 原始对象}。结构错误直接抛，坏条目记进质量报告。"""
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "{} JSON 解析失败（第 {} 行第 {} 列）：{}".format(path.name, exc.lineno, exc.colno, exc.msg)
        )

    # 兼容两种常见形态：[...] 和 {"orders": [...]}
    if isinstance(data, dict) and isinstance(data.get("orders"), list):
        data = data["orders"]
    if not isinstance(data, list):
        raise ValueError("{} 顶层应该是数组，实际是 {}".format(path.name, type(data).__name__))

    index: Dict[str, Dict[str, Any]] = {}
    for i, item in enumerate(data, 1):
        quality.json_items += 1
        if not isinstance(item, dict):
            quality.reject(BadRow(path.name, i, "json_item_not_object", repr(item)[:120]))
            continue

        order_id = str(item.get("order_id") or "").strip()
        if not order_id:
            quality.reject(BadRow(path.name, i, "json_missing_order_id", json.dumps(item, ensure_ascii=False)[:120]))
            continue
        if order_id in index:
            quality.reject(BadRow(path.name, i, "json_duplicate_order_id", order_id))
            continue

        index[order_id] = item

    return index


# --------------------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------------------
def parse_log(path: Path, limit: int = DEFAULT_MAX_ERRORS) -> LogStats:
    stats = LogStats()
    # errors="replace"：日志里混进非法字节也不至于整个脚本挂掉
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            stats.lines += 1
            line = raw.rstrip("\r\n")

            if not line.strip():
                stats.blank += 1
                continue

            match = LOG_LINE_RE.match(line)
            if match is None:
                stats.unparsed += 1
                continue

            stats.parsed += 1
            level = match.group("level")
            stats.levels[level] = stats.levels.get(level, 0) + 1

            if level in ("ERROR", "FATAL"):
                stats.errors_total += 1
                if len(stats.errors) < limit:  # 只留前 N 条，避免报告无限膨胀
                    stats.errors.append(
                        {
                            "line": lineno,
                            "ts": match.group("ts"),
                            "event": match.group("event"),
                            "fields": {k: v.strip('"') for k, v in KV_RE.findall(match.group("rest") or "")},
                        }
                    )

    return stats


# --------------------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------------------
def _bucket_rows(bucket: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {"key": key, "orders": val["orders"], "qty": val["qty"], "amount": val["amount"]}
        for key, val in sorted(bucket.items())
    ]


def summarize(
    orders: List[Order],
    meta: Dict[str, Dict[str, Any]],
    quality: Quality,
    logs: LogStats,
) -> Dict[str, Any]:
    by_region: Dict[str, Dict[str, Any]] = {}
    by_product: Dict[str, Dict[str, Any]] = {}
    status_counts: Dict[str, int] = {}
    missing_meta: List[str] = []
    total_qty = 0
    total_amount = 0.0

    for order in orders:
        total_qty += order.qty
        total_amount += order.amount

        for bucket, key in ((by_region, order.region), (by_product, order.product)):
            entry = bucket.setdefault(key, {"orders": 0, "qty": 0, "amount": 0.0})
            entry["orders"] += 1
            entry["qty"] += order.qty
            entry["amount"] = round(entry["amount"] + order.amount, 2)

        info = meta.get(order.order_id)
        if info is None:
            missing_meta.append(order.order_id)
        else:
            status = str(info.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1

    csv_ids = {order.order_id for order in orders}
    orphan_meta = sorted(set(meta) - csv_ids)

    return {
        "schema_version": SCHEMA_VERSION,
        "totals": {"orders": len(orders), "qty": total_qty, "amount": round(total_amount, 2)},
        "by_region": _bucket_rows(by_region),
        "by_product": _bucket_rows(by_product),
        "metadata": {
            "matched": len(orders) - len(missing_meta),
            "missing": sorted(missing_meta),
            "orphan": orphan_meta,
            "status_counts": dict(sorted(status_counts.items())),
        },
        "logs": {
            "lines": logs.lines,
            "blank": logs.blank,
            "parsed": logs.parsed,
            "unparsed": logs.unparsed,
            "levels": dict(sorted(logs.levels.items())),
            "errors_total": logs.errors_total,
            "errors": logs.errors,
        },
        "quality": {
            "csv_lines": quality.csv_lines,
            "json_items": quality.json_items,
            "accepted": quality.accepted,
            "rejected": quality.rejected,
            "reasons": dict(sorted(quality.reasons.items())),
        },
    }


# --------------------------------------------------------------------------------------
# 渲染：拼字符串是纯函数，好测；写盘单独交给 write_atomic
# --------------------------------------------------------------------------------------
def render_json(payload: Dict[str, Any]) -> str:
    # sort_keys + 固定缩进 => 相同输入总是相同字节
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def render_summary_csv(summary: Dict[str, Any]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")  # 固定 \n，避免 Windows 上 CRLF 造成字节差异
    writer.writerow(["section", "key", "orders", "qty", "amount"])

    totals = summary["totals"]
    writer.writerow(["totals", "ALL", totals["orders"], totals["qty"], "{:.2f}".format(totals["amount"])])
    for section, rows in (("by_region", summary["by_region"]), ("by_product", summary["by_product"])):
        for row in rows:
            writer.writerow([section, row["key"], row["orders"], row["qty"], "{:.2f}".format(row["amount"])])
    return buf.getvalue()


def render_rejected(rows: List[BadRow]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["source", "line", "reason", "raw"])
    for row in sorted(rows, key=lambda r: (r.source, r.line)):
        writer.writerow([row.source, row.line, row.reason, row.raw])
    return buf.getvalue()


# --------------------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------------------
def run(input_dir: Path, output_dir: Path, dry_run: bool = False, max_errors: int = DEFAULT_MAX_ERRORS) -> Dict[str, Any]:
    sales_path = input_dir / "sales.csv"
    meta_path = input_dir / "orders.json"
    log_path = input_dir / "app.log"
    for path in (sales_path, meta_path, log_path):
        if not path.is_file():
            raise FileNotFoundError("找不到输入文件: {}".format(path))

    quality = Quality()
    orders = load_orders_csv(sales_path, quality)
    logger.info("CSV 读入 %d 行，接收 %d 单，拒绝 %d 行", quality.csv_lines, quality.accepted, quality.rejected)

    meta = load_orders_json(meta_path, quality)
    logger.info("JSON 读入 %d 条元数据", len(meta))

    logs = parse_log(log_path, limit=max_errors)
    logger.info("日志 %d 行：解析 %d，无法解析 %d", logs.lines, logs.parsed, logs.unparsed)

    summary = summarize(orders, meta, quality, logs)

    if dry_run:
        logger.info("--dry-run：不写任何文件")
        return summary

    write_atomic(output_dir / "summary.json", render_json(summary))
    write_atomic(output_dir / "summary.csv", CSV_BOM + render_summary_csv(summary))
    write_atomic(output_dir / "rejected.csv", CSV_BOM + render_rejected(quality.bad_rows))
    logger.info("已写入 %s", output_dir)
    return summary


def setup_logging(log_file: Optional[Path], verbose: bool) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s")

    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(level)
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CSV + JSON + 日志 -> 可重复生成的订单汇总报告",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input-dir", type=Path, default=Path("data"), help="输入目录")
    parser.add_argument("--output-dir", type=Path, default=Path("out"), help="输出目录")
    parser.add_argument("--log-file", type=Path, default=None, help="运行日志路径，默认 <output-dir>/_logs/etl.log")
    parser.add_argument("--dry-run", action="store_true", help="只计算不写盘")
    parser.add_argument("--max-errors", type=int, default=DEFAULT_MAX_ERRORS, help="报告里最多保留多少条 ERROR")
    parser.add_argument("--fail-on-bad", action="store_true", help="坏行比例超阈值时以退出码 2 结束")
    parser.add_argument("--max-bad-ratio", type=float, default=0.25, help="坏行比例阈值")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印 DEBUG 级日志")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # dry-run 承诺“什么都不写”，所以连日志目录都不建
    log_file = args.log_file
    if log_file is None and not args.dry_run:
        log_file = args.output_dir / "_logs" / "etl.log"
    setup_logging(log_file, args.verbose)

    try:
        summary = run(args.input_dir, args.output_dir, dry_run=args.dry_run, max_errors=args.max_errors)
    except (OSError, ValueError) as exc:
        logger.error("运行失败：%s", exc)
        return 1

    totals = summary["totals"]
    quality = summary["quality"]
    considered = quality["csv_lines"] + quality["json_items"]
    ratio = quality["rejected"] / considered if considered else 0.0

    print("订单 {} 单，数量 {}，金额 {:.2f}".format(totals["orders"], totals["qty"], totals["amount"]))
    print("坏行 {}/{}（{:.1%}），原因：{}".format(
        quality["rejected"], considered, ratio,
        ", ".join("{}={}".format(k, v) for k, v in quality["reasons"].items()) or "无",
    ))
    if not args.dry_run:
        print("输出：{}".format(args.output_dir.resolve()))

    if args.fail_on_bad and ratio > args.max_bad_ratio:
        logger.error("坏行比例 %.1f%% 超过阈值 %.1f%%", ratio * 100, args.max_bad_ratio * 100)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
