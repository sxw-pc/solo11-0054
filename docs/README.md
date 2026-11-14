# SensorAlign 测点对齐台

SensorAlign 是一个**离线、确定性**的多测点时间基准对齐与重采样仲裁工具。它读取本地
CSV/JSONL 测点记录和 YAML 对齐契约，在整数微秒时间轴上完成时钟偏移估计、缺口识别、
重采样与质量报告，并把输入指纹、对齐格和事件写入 SQLite 台账。

## 项目定位
**是：**

- 面向工业/物联网时序采集的离线批处理工具。
- 精确整数微秒时间戳：单调性校验、去重、冲突标记、缺口识别。
- 四种重采样策略：`linear` / `step` / `mean` / `lag_window`，缺失原因显式标记。
- 基于锚点事件的中位数偏移和 Theil-Sen 稳健漂移估计。
- 可复现 JSON / Markdown 报告，以及含输入指纹和输出溯源的 SQLite 台账。

**不是：**

- 实时流处理、消息队列或数据库服务。
- 通用时序数据库、指标可视化看板。
- DTW、机器学习对齐、多物理量联合相关性分析。
- 自动采集真实设备数据；本工具只读取本地文件。

## 快速上手
要求 Python 3.10+、`PyYAML` 与 `pytest`，运行期不需要网络。

```bash
# 1. 用内置合成 fixture 跑通全流程，生成 out/demo.db、out/demo.json、out/demo.md
python -m sensoralign demo

# 2. 按契约执行对齐，生成 out/basic.{json,md} 与 out/align.db
python -m sensoralign run --contract fixtures/contracts/basic.yaml

# 3. 从台账重放 Markdown 报告（也可 --format json --out path）
python -m sensoralign report --db out/demo.db --format markdown

# 4. 运行全部测试
python -m pytest -q
```

`run` 的 `--out-dir` 默认是 `out/`。`report` 不传 `--out` 时输出到标准输出；
台账内有多条运行时可用 `--run-id` 指定，否则选择最后写入的一条。

## 契约与字段
契约完整字段、默认值、策略语义与错误码见 [CONTRACT_SPEC.md](CONTRACT_SPEC.md)。

最小示例：

```yaml
version: 1
name: basic
inputs:
  - id: dev-a1
    path: fixtures/series/dev-a1.csv
    format: csv
    device_id: dev-a1
    metric: temperature
grid:
  origin_us: 1700000000000000
  step_us: 1000000
  end_us: 1700000060000000
max_gap_us: 2500000
monotonic_policy: sort_stable
clock_skew: none
resample:
  default: linear
```

相对路径优先按契约所在仓库的 `pyproject.toml` 根目录解析，再依次尝试契约文件目录和当前工作目录。

## Fixtures
所有 fixture 均为合成数据，设备标识仅使用 `dev-a1` 等合成名称。

| 文件 | 场景 |
| --- | --- |
| `fixtures/series/dev-a1.csv` | 1 秒正常基准序列 |
| `fixtures/series/dev-b2.csv` | 2 秒间隔，时钟恒定快 2 秒 |
| `fixtures/series/dev-c3.jsonl` | JSONL，1 秒间隔，线性漂移 +100 µs/s |
| `fixtures/series/dev-d4_dup.csv` | 重复点、同时间戳冲突、乱序点 |
| `fixtures/series/dev-e5_gap.csv` | 20–30 秒间缺采，时钟偏移 +300 ms |
| `fixtures/anchor/anchors.yaml` | 5 个跨设备公共锚点事件 |
| `fixtures/anchor/anchors_sparse.yaml` | 仅 2 个锚点，演示 `insufficient` |
| `fixtures/contracts/basic.yaml` | 五路、显式网格、三种重采样策略 |
| `fixtures/contracts/drift.yaml` | 线性漂移 + `grid: from_union` |
| `fixtures/contracts/gap_policy.yaml` | 缺口阈值与 step 策略 |
| `fixtures/contracts/sparse_anchor.yaml` | 锚点不足，不执行时钟修正 |

`basic` 的固定结果便于端到端断言：60 个网格格、5 路序列、公共有值格 42、
对齐完成率 0.700000；`dev-d4` 去除 1 个重复点并记录 1 个冲突；
`dev-e5` 有 1 个缺口、9 个 `in_gap` 格。

## 输出示例（截断）
`out/demo.json`：

```json
{
  "alignment": {
    "common_cells": 42,
    "completion_rate": 0.7,
    "missing_any_cells": 18,
    "total_cells": 60
  },
  "grid": {
    "cell_count": 60,
    "end_us": 1700000060000000,
    "mode": "explicit",
    "origin_us": 1700000000000000,
    "step_us": 1000000
  },
  "schema_version": "1.0"
}
```

Markdown 报告包含项目摘要、契约快照、每路质量表、时钟偏移估计表、缺口清单和
事件/错误清单。缺口与错误清单按 `gap_report_limit` 截断并标注总数。

SQLite 台账固定包含五张表：`run`、`series`、`aligned`、`metrics`、`errors`。
`aligned` 每个系列每格一行，保存 `grid_index`、`ts_us`、`value`、`marker`。

## 确定性与脱敏
- 时间戳与网格全部使用整数微秒，不参与浮点比较。
- JSON 使用稳定键排序、UTF-8 与固定缩进；Markdown 表格顺序固定。
- 报告不写运行时间戳、主机名、绝对路径或随机值。
- 同一输入文件与同一契约重复运行，JSON 报告字节一致。
- `run` 表只保存运行 id、契约哈希和结果哈希。
- 输入文件 SHA-256 写入 `series` 表；每个对齐格写入台账。

## 已知边界
- 仅支持批处理，不支持实时流、增量更新和分布式执行。
- 每个文件只能包含一个 `device_id + metric` 流。
- 浮点时间戳、指数形式时间戳会被拒绝；值必须是有限浮点数。
- 不处理时区、闰秒或本地夏令时，时间统一为 epoch 微秒。
- 时钟模型以一个契约参考设备为基准，最多一个线性漂移项。
- 锚点少于 3 个时状态为 `insufficient`，不会猜测偏移。
- `from_union` 使用各路覆盖区间交集与最小正相邻间隔；无法推断步长时报错。
- 网格上限 1,000,000 格，避免无意中耗尽内存。
- `mean` 使用以网格点为中心、宽度为一个网格步长的半开窗口；`lag_window`
  使用右端对齐网格时刻的固定滞后窗口 `[t - step_us, t)`，奇数步长无半格偏差。
- 不做 DTW、相关性分析、前端图表或外部数据库集成。

## 文档与合规
- 契约规范：[CONTRACT_SPEC.md](CONTRACT_SPEC.md)
- 离线与数据使用约束：[COMPLIANCE.md](COMPLIANCE.md)
