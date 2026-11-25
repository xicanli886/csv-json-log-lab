# CSV / JSON / 日志 数据处理脚本（可重复运行）

一个不到 400 行、只依赖 Python 标准库的小项目：把「一份脏 CSV + 一份 JSON 元数据 + 一份半结构化日志」合成为三份报告，并且做到**同样的输入跑多少次，输出都逐字节相同**。

这是给你学 Python 数据处理用的样板 —— 每个设计选择都有理由，代码里的注释写的就是理由。

## 快速开始

```powershell
cd csv-json-log-lab
python src/etl.py                      # 读 data/，写 out/
python src/etl.py --dry-run            # 只算不写，看汇总
python src/etl.py --fail-on-bad        # 坏行超过 25% 就以退出码 2 失败（适合放进定时任务）
python -m unittest discover -s tests -v   # 13 个单测
```

> Windows 控制台如果是 GBK，输出中文会花。先执行 `chcp 65001`，或者设 `$env:PYTHONIOENCODING="utf-8"`。

本机装的是 Python 3.8.10（已停止维护），代码特意只用 3.8 能跑的语法 —— 不是推荐，是保证你现在就能跑。有条件升到 3.11+ 更省心。

## 它产出什么

`out/summary.csv`：

```
section,key,orders,qty,amount
totals,ALL,10,31,9826.50
by_region,华东,4,12,2140.50
by_region,华北,3,13,4792.00
by_region,华南,3,6,2894.00
by_product,显示器,3,4,5196.00
...
```

`out/summary.json` 给程序读：总计、分区汇总，外加 `metadata`（匹配上的/缺失的/孤儿订单）、`logs`（各级别条数、ERROR 明细）、`quality`（收了多少行、拒了多少行、每种原因各几条）。

`out/rejected.csv` 给人工排查：`source, line, reason, raw` —— 哪一行、为什么被拒、原文长什么样。

## 目录

```
csv-json-log-lab/
├── data/
│   ├── sales.csv      # 16 行，掺了 6 种脏数据
│   ├── orders.json    # 11 条，掺了 1 条缺主键的
│   └── app.log        # 11 行，掺了 1 行格式不对的
├── src/etl.py         # 全部逻辑
├── tests/test_etl.py  # 13 个单测
└── out/               # 生成物（可以随时删，删了重跑就行）
```

## 「可重复运行」到底指什么

这五个性质是分开的，每个都要单独设计：

**1. 确定性** —— 相同输入 → 字节相同的输出。
所有聚合结果都按 key 排序（`_bucket_rows`、`sorted(...)`），JSON 用 `sort_keys=True` 且**不写运行时间戳**，CSV 固定 `lineterminator="\n"`（Windows 默认是 `\r\n`，会导致同一个脚本在两台机器上产出不同字节）。

**2. 幂等** —— 跑 100 次和跑 1 次一样。
每次覆盖而不是追加。反例：`open(..., "a")` 追加写，重跑就把数据写重了。

**3. 原子性** —— 中途崩溃不留半成品。
`write_atomic()`：同目录建临时文件 → `flush` → `fsync` → `os.replace()`。`os.replace` 在同一分区内是原子操作，读的人要么看到旧文件，要么看到新文件，不会看到写了一半的。

**4. 参数化** —— 不改代码就能换输入输出。
`argparse` 提供 `--input-dir` / `--output-dir` / `--fail-on-bad` 等。反例：路径硬编码在代码里，换台机器就得改。

**5. 可测** —— 逻辑是纯函数，`main()` 只接线。
`parse_order_row()` 收一个 dict 返回 `(Order, 原因)`，不碰磁盘；`run()` 负责编排；`main()` 只管参数和退出码。所以单测里可以随便造数据。

自己验证一下第 1 条：

```powershell
python src/etl.py; (Get-FileHash out\summary.json).Hash
python src/etl.py; (Get-FileHash out\summary.json).Hash   # 两次必须一样
```

## 知识点地图

| 你学的 | 代码里在哪 | 一句话要点 |
|---|---|---|
| `csv.DictReader` | `load_orders_csv` | 按列名取值比按下标稳；`reader.line_num` 是真实行号，报错时能指回原文 |
| `csv.writer` | `render_summary_csv` | 写 CSV 永远用它，别手拼字符串 —— 字段里有逗号/引号就废了 |
| `utf-8-sig` | `load_orders_csv` | 吃掉 Excel 存的 BOM，否则第一列名会变成 `\ufefforder_id` |
| `newline=""` | 读写 CSV 处 | 不加它，Windows 上会出现空行 |
| `json.loads` / `dumps` | `load_orders_json` / `render_json` | `ensure_ascii=False` 才让中文不变成 `\u4e2d` |
| `json.JSONDecodeError` | 同上 | 自带 `lineno`/`colno`，报错能精确定位 |
| 日志解析 | `parse_log` + `LOG_LINE_RE` | 结构化日志的正确姿势是**先约定格式**，再用正则拆；半结构化日志只能尽力而为，拆不出来就计数 |
| `logging` | `setup_logging` | 两个 handler：stderr 给人看，文件留档；级别用 `-v` 控制 |
| `argparse` | `build_parser` | `ArgumentDefaultsHelpFormatter` 自动把默认值写进 `--help` |
| `pathlib.Path` | 全篇 | `Path / "x"` 拼路径，比字符串拼接安全 |
| `dataclass` | `Order` / `BadRow` / `Quality` | 字段名拼错立刻 `AttributeError`，比字典 `row["qtyy"]` 早暴露 |
| `@dataclass(frozen=True)` | `Order` | 不可变值对象，顺手可哈希 |
| 原子写 | `write_atomic` | `mkstemp` + `fsync` + `os.replace` 三件套 |
| `math.isfinite` | `parse_order_row` | `float("nan")` 能通过 `float()`，但会让后续求和全变 `nan` |
| `unittest` | `tests/test_etl.py` | `TemporaryDirectory` 保证测试不污染工作区 |

## 数据里埋的三个坑

1. **` A1004 , 2026-11-02 , ...`** —— 手工从 Excel 复制出来的行，字段前后带空格。代码里 `(v or "").strip()` 统一清掉。不处理的话，`order_id` 会变成 `" A1004 "`，和 JSON 里的 `A1004` 对不上，凭空多出一条「缺失元数据」。
2. **`A1015,2026-02-30,...`** —— 正则 `^\d{4}-\d{2}-\d{2}$` 认为它合法，`datetime.strptime` 会拒绝。这就是为什么校验用 `strptime` 而不是正则。
3. **`A1003` 出现两次** —— 主键重复。策略是「先到先得，后面的记为拒绝」，并在 `rejected.csv` 里留痕。也可以改成按时间取最新，但**必须明确选一种**，否则同一条订单会被算两遍。

## 练习（从易到难）

1. **加一列**：`by_region` 里加「客单价」= amount / orders，保留两位小数。注意分母为 0。
2. **换主键策略**：重复 `order_id` 时保留**金额大**的那条，被淘汰的仍写进 `rejected.csv`（reason 用 `superseded_duplicate`）。改完记得加单测。
3. **增量处理**：加 `--since 2026-11-03`，只统计该日期之后的订单，并用 `--state state.json` 记住上次处理到的最大日期。想清楚：重跑同一天会不会重复计数？
4. **日志关联**：从 `app.log` 的 ERROR 行里抽出 `order_id`，在 `summary.json` 里多输出一段 `orders_with_errors`，把日志和业务数据串起来。
5. **换成流式**：现在 CSV 是全量读进内存的。改成生成器（`yield`），做到一行一行处理 —— 想想汇总部分要怎么配合。
6. **上 pandas**：用 `pd.read_csv(..., dtype=str)` 重写一版，对比 200 行标准库版本和 20 行 pandas 版本各自的可控点在哪。脏数据多的时候，你更信任哪个？

## 常见坑（都是真会踩的）

- **浮点求和**：`0.1 + 0.2 != 0.3`。金额场景正经做法是用 `decimal.Decimal` 或全程「分」为单位存整数。这里为了简单用 `round(..., 2)`，够用但不严谨。
- **`os.replace` 跨分区会失败**：临时文件必须建在**目标同目录**，代码里 `dir=str(path.parent)` 就是这个原因。
- **`open` 不指定 `encoding`**：Windows 上默认是 `cp936`，读 UTF-8 文件直接 `UnicodeDecodeError`。永远显式写 `encoding="utf-8"`。
- **CSV 里的 BOM**：两份 CSV 报告都加了 `\ufeff` 开头。不加的话，Excel 双击打开中文全是乱码（用 `utf-8-sig` 读回来则无感）。这是 Windows + Excel 场景的实用妥协。
- **日志文件本身别放进确定性输出目录**：`out/_logs/etl.log` 带时间戳，每跑一次都不同，它不算「报告」。
- **时区**：日志里的 `+08:00` 是文本，脚本没做时区换算。真要跨时区比时间，先统一转 UTC 再比。

## 下一步可以往哪走

- 数据量上去了 → 换 `pandas` 或 `polars`，或直接上 `sqlite3` 做聚合
- 要跑在服务器上 → 加 `--fail-on-bad`，再用 Windows 计划任务 / cron 定时跑，失败告警
- 要给人看 → 用 DSH 这边的 office 能力把 `summary.json` 直接渲染成 Excel 报表
