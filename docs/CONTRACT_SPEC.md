# 对齐契约规范（version 1）

契约是 SensorAlign 的唯一策略来源：输入文件、时间轴、时间戳卫生策略、缺口阈值、
时钟模型和重采样策略均由 YAML 声明。未知字段会被拒绝，避免拼写错误被静默忽略。

## 1. 顶层字段
| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `version` | int | 是 | - | 固定为 `1`。 |
| `name` | str | 是 | - | 稳定名称，匹配 `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`，用于输出文件名和 run 内容。 |
| `description` | str | 否 | `""` | 契约说明，进入脱敏快照。 |
| `inputs` | list | 是 | - | 至少一个输入流，详见第 2 节。 |
| `anchors` | str | 条件 | `null` | 锚点 YAML 路径；`clock_skew != none` 时必填。 |
| `grid` | mapping/str | 是 | - | 显式网格或字符串 `from_union`，详见第 3 节。 |
| `max_gap_us` | int | 否 | `2000000` | 缺口阈值，必须 > 0；相邻间隔严格大于该值才算缺口。 |
| `monotonic_policy` | str | 否 | `reject` | `reject` / `sort_stable` / `drop_late`。 |
| `duplicate_policy` | str | 否 | `keep_first` | `keep_first` / `keep_last` / `reject`。 |
| `conflict_policy` | str | 否 | `keep_first` | 同时间戳不同值：`keep_first` / `reject`。 |
| `clock_skew` | str | 否 | `none` | `none` / `constant` / `linear`。 |
| `resample` | mapping | 否 | `{default: linear}` | 默认策略和按 series id 的覆盖。 |
| `gap_report_limit` | int | 否 | `20` | 报告中缺口和事件清单的最大展示条数。 |

顶层未列出的字段触发 `E_CONTRACT_UNKNOWN`。`version` 不是 1、布尔值冒充整数、
空字符串、非法枚举均会失败。

## 2. `inputs` 条目
| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | str | 是 | 系列稳定标识，全局唯一；用于 `resample.overrides`。 |
| `path` | str | 是 | 本地 CSV/JSONL 路径；禁止网络地址。 |
| `format` | str | 否 | `csv` 或 `jsonl`；省略时按 `.csv` / `.jsonl` / `.ndjson` 推断。 |
| `device_id` | str | 是 | 期望设备标识；每一行必须与之相同。 |
| `metric` | str | 是 | 期望物理量名称；每一行必须与之相同。 |

一个文件只能包含一个 `(device_id, metric)` 流；混入多流会报
`E_SERIES_AMBIGUOUS`。输入必填列/字段为 `ts_us`、`device_id`、`metric`、`value`；
额外列忽略。CSV 支持 UTF-8 与 UTF-8-SIG，JSONL 支持逐行 JSON 对象。

相对路径的解析顺序：先以最近的、包含 `pyproject.toml` 的祖先目录为项目根，
再以契约文件所在目录、当前工作目录为备选根；绝对路径原样使用。

## 3. `grid`
### 3.1 显式网格
```yaml
grid:
  origin_us: 1700000000000000
  step_us: 1000000
  end_us: 1700000060000000
```

- 三个字段均为整数微秒，`step_us > 0`，`origin_us < end_us`。
- `end_us` 为**排他上界**：网格为 `origin, origin+step, ... < end`。
- 网格格数上限 1,000,000；超限报 `E_GRID_TOO_LARGE`。

### 3.2 交集网格
```yaml
grid: from_union
```

构建规则固定如下：

1. 对每一路时钟修正后的数据取首个和末个观测。
2. 交集起点 `origin_us = max(各系列首点)`，交集终点 = `min(各系列末点)`。
3. 若任一路为空或无交集，报 `E_GRID_NO_OVERLAP`。
4. 步长 `step_us = min(各系列最小正相邻间隔)`；无法推断时报 `E_GRID_NO_STEP`。
5. 网格从交集起点按步长递增，只保留 `<= 交集终点` 的时刻。
6. 报告中的 `end_us = 交集终点 + 1`，仍表示排他上界。

## 4. 时间戳卫生
### 4.1 单调性
| 策略 | 语义 |
| --- | --- |
| `reject` | 遇到相邻逆序立即报 `E_TS_NOT_MONOTONIC`。 |
| `sort_stable` | 按 `ts_us` 稳定排序，保留同时间戳的输入相对顺序；每个逆序点写 `W_TS_OUT_OF_ORDER`。 |
| `drop_late` | 保持输入顺序，丢弃时间戳小于历史最大值的迟到点；每个丢弃写 `W_TS_OUT_OF_ORDER`。 |

### 4.2 重复与冲突
- 完全相同 `(ts_us, value)` 为重复。
- 同 `ts_us` 但 `value` 不同为冲突。
- `keep_first` / `keep_last` 决定重复点保留哪条；`reject` 报
  `E_TS_DUPLICATE`。
- 冲突默认保留最先出现的值，每个被丢弃的冲突写 `W_TS_CONFLICT`；`reject`
  报 `E_TS_CONFLICT`。
- 去掉重复/冲突后，每个时间戳最多保留一个点。

### 4.3 缺口
对时钟修正并去重后的相邻点，若 `right.ts_us - left.ts_us > max_gap_us`，记录一个
缺口。缺口在台账中体现为事件，在重采样结果中标记 `in_gap`。网格时刻严格位于
缺口两端之间时才是 `in_gap`，端点上的真实观测仍为 `ok`。

## 5. 时钟偏移与漂移
`anchors` 文件格式：

```yaml
version: 1
reference_device: dev-a1
events:
  - id: evt-001
    observations:
      dev-a1: 1700000000000000
      dev-b2: 1700000002000000
```

规则：

- `reference_device` 必须出现在至少一个事件中。
- 每个事件至少包含两个设备的整数微秒观测；事件 id 唯一。
- 未列出的字段、非整数时间戳、重复事件 id 均报 `E_ANCHOR_*`。
- 对每个设备，取与参考设备的共同事件。原始偏移 `device_ts - reference_ts`。
- 锚点数 `< 3`：状态 `insufficient`，不执行修正，并写 `W_OFFSET_INSUFFICIENT`。
- 状态枚举：`reference`（参考设备）、`disabled`（`clock_skew: none`）、
  `no_anchors`、`insufficient`、`degenerate`、`ok`。

模型：

| `clock_skew` | 估计 |
| --- | --- |
| `none` | 不做修正。 |
| `constant` | 偏移取所有共同事件原始偏移的中位数；斜率为 0。 |
| `linear` | Theil-Sen 斜率 = 所有事件对 `(Δoffset / Δreference_seconds)` 的中位数；截距为相对首个参考锚点的偏移中位数。 |

置信信息进入报告：`anchor_count`、`median_offset_us`、`intercept_us`、
`slope_us_per_s`、`residual_median_us`。线性修正使用：

```text
t_ref = (t_dev - intercept + slope * origin / 1e6) / (1 + slope / 1e6)
```

结果四舍五入（half-away-from-zero）为整数微秒。若分母接近 0，状态为
`degenerate`，不应用修正。

## 6. 重采样
```yaml
resample:
  default: linear
  overrides:
    dev-b2: step
    dev-c3: mean
```

`default` 和每个 `overrides` 值只能是 `linear`、`step`、`mean`。`overrides`
的键必须是已声明的 series id，否则报 `E_CONTRACT_VALUE`。

| 策略 | 语义 | 标记 |
| --- | --- | --- |
| `linear` | 网格时刻恰有观测则取值；否则左右都有观测才线性插值。 | 缺左侧 `no_left`，缺右侧 `no_right`，横跨大缺口 `in_gap`。 |
| `step` | 取时间戳 `<=` 网格时刻的最近观测；之后持续保持。 | 首点之前 `no_left`，缺口内部 `in_gap`。 |
| `mean` | 取窗口内观测的算术平均。窗口为 `[t - step//2, t + (step - step//2))`，宽度等于网格步长。 | 窗口无点且位于数据范围中间 `no_window`；范围外 `no_left` / `no_right`；缺口内部 `in_gap`。 |

标记集合固定为 `ok`、`no_left`、`no_right`、`in_gap`、`no_window`，计数顺序固定。
`ok` 才写入数值；其他标记的 `value` 为 `NULL`。不允许静默填充。

## 7. 质量指标
每路：

- `total_cells`、`value_cells`、`coverage = value_cells / total_cells`（6 位小数）。
- `gap_cells`、`gap_ratio = gap_cells / total_cells`。
- `conflict_count`、`gap_count`、`gap_duration_us`。
- `marker_counts`，包含五种标记的零值。

跨路：

- `common_cells`：所有系列均为 `ok` 的网格格数。
- `missing_any_cells`：至少一路非 `ok` 的格数。
- `completion_rate = common_cells / total_cells`。

注意：`completion_rate` 始终按**每个 series 是否都有值**统计，与设备宽表无关；
把同一设备的多个 metric 合并成宽表行不会改变该值。

空网格或零分母返回 `0.0`，不抛除零错误。

### 7.1 设备宽表

报告中的 `devices` 是按 `device_id` 聚合的视图，与 `series` 平铺表并存：

- 每个设备有 `columns`，列出该设备声明的每个 metric：`key`、`metric`、`series_id`、`strategy`。
- `cells` 与 `grid` 逐格对齐：`grid_index`、`ts_us`、`values`、`markers`。
- `values` 和 `markers` 都是以 column `key` 为键的映射。某 metric 在该格无观测时，
  `values[key]` 为 `null`，`markers[key]` 沿用原有的 `ok` / `no_left` / `no_right` /
  `in_gap` / `no_window`，不会产生空字符串或新造填充值。
- 同一设备重复声明同名 metric 时，列键为 `metric@series_id`，避免覆盖。
- 宽表是从账本 `series` + `aligned` 行重放出来的纯视图，不新增台账表，也不改变 `aligned`
  的每 series/每格一行结构。

## 8. 报告与台账
`run` 生成：

- `<out-dir>/<contract-name>.json`：稳定排序 JSON，UTF-8，末尾换行。
- `<out-dir>/<contract-name>.md`：Markdown 报告。
- `<out-dir>/align.db`：SQLite 台账。

`demo` 固定使用 `fixtures/contracts/basic.yaml`，生成 `out/demo.db`、
`out/demo.json`、`out/demo.md`。

台账五表：

| 表 | 内容 |
| --- | --- |
| `run` | `run_id`、`contract_sha256`、`result_sha256`，不含运行时间戳。 |
| `series` | 输入路径/格式/SHA-256、点数、去重/冲突、时钟估计、缺口统计。 |
| `aligned` | 每个 series、每个 grid index 一行：`ts_us`、`value`、`marker`。 |
| `metrics` | 契约快照、网格、对齐指标、每路质量、偏移指标。 |
| `errors` | 有序审计事件，含 `W_*` 警告，`detail_json` 保存结构化细节。 |

报告中的设备宽表（`devices`）由 `series` 与 `aligned` 重建，不占用额外表；
`report --format json` 重放结果与 `run` 生成的 JSON 字节一致。

## 9. 退出码
| 退出码 | 含义 |
| --- | --- |
| `0` | 成功。 |
| `2` | 契约错误（`E_CONTRACT_*`）。 |
| `3` | 数据错误（`E_TS_*`、`E_FIELD_*`、`E_ANCHOR_*`、`E_GRID_*` 等）。 |
| `1` | 台账/其他内部或 I/O 错误。 |

## 10. 错误码与警告码
### 契约
| 代码 | 含义 |
| --- | --- |
| `E_CONTRACT_READ` | 契约文件不可读。 |
| `E_CONTRACT_YAML` | YAML 解码失败。 |
| `E_CONTRACT_TYPE` | 字段类型错误。 |
| `E_CONTRACT_MISSING` | 缺少必填字段，或非 none 时钟模型缺 anchors。 |
| `E_CONTRACT_UNKNOWN` | 未知顶层/input/resample 字段。 |
| `E_CONTRACT_DUPLICATE` | 重复系列 id。 |
| `E_CONTRACT_GRID` | 网格字段缺失、类型错误或范围非法。 |
| `E_CONTRACT_VALUE` | 枚举、名称、格式或 overrides 取值非法。 |

### 加载与值
| 代码 | 含义 |
| --- | --- |
| `E_INPUT_READ` | 输入文件不可读。 |
| `E_FORMAT` | 不支持的文件格式。 |
| `E_ENCODING` | 不是合法 UTF-8/UTF-8-SIG。 |
| `E_JSON` | JSONL 行不是合法 JSON 对象。 |
| `E_COLUMN_MISSING` | CSV 缺少必填列。 |
| `E_FIELD_MISSING` | 行缺少必填字段。 |
| `E_FIELD_TYPE` | 设备/物理量等文本字段类型错误。 |
| `E_FIELD_EMPTY` | 必填文本为空。 |
| `E_DEVICE_MISMATCH` | 行的设备与契约不符。 |
| `E_METRIC_MISMATCH` | 行的物理量与契约不符。 |
| `E_SERIES_AMBIGUOUS` | 一个文件含多个设备/物理量流。 |
| `E_VALUE_TYPE` | 值不是数字。 |
| `E_VALUE_NOT_FINITE` | 值为 NaN 或无穷。 |
| `E_EMPTY_INPUT` | 文件没有任何观测。 |

### 时间戳
| 代码 | 含义 |
| --- | --- |
| `E_TS_FLOAT` | 时间戳为浮点或浮点表示。 |
| `E_TS_EMPTY` | 时间戳为空。 |
| `E_TS_NOT_INT` | 时间戳含非整数字符。 |
| `E_TS_NOT_MONOTONIC` | `reject` 策略下出现逆序。 |
| `E_TS_DUPLICATE` | `reject` 策略下出现重复。 |
| `E_TS_CONFLICT` | `reject` 策略下同时间戳值冲突。 |

### 锚点、网格、台账
| 代码 | 含义 |
| --- | --- |
| `E_ANCHOR_READ` | 锚点文件不可读。 |
| `E_ANCHOR_YAML` | 锚点 YAML 解码失败。 |
| `E_ANCHOR_TYPE` | 锚点根或事件类型错误。 |
| `E_ANCHOR_MISSING` | 缺少锚点必填字段。 |
| `E_ANCHOR_UNKNOWN` | 未知锚点字段。 |
| `E_ANCHOR_VALUE` | 事件、设备或时间戳取值非法，或参考设备缺失。 |
| `E_GRID_NO_OVERLAP` | `from_union` 无覆盖或交集。 |
| `E_GRID_NO_STEP` | 无法推断 `from_union` 步长。 |
| `E_GRID_EMPTY` | 显式网格无格。 |
| `E_GRID_TOO_LARGE` | 网格超过 1,000,000 格。 |
| `E_LEDGER_READ` | 台账不存在或不可读。 |
| `E_LEDGER_WRITE` | 台账写入失败。 |
| `E_LEDGER_RUN_NOT_FOUND` | 指定 run 不存在。 |
| `E_LEDGER_CORRUPT` | 缺表或缺少重建报告所需指标。 |
| `E_INTERNAL` | CLI 最终防线捕获的未归类错误。 |

### 警告（写入 `errors` 表）
| 代码 | 含义 |
| --- | --- |
| `W_TS_OUT_OF_ORDER` | 乱序点被排序或丢弃。 |
| `W_TS_DUPLICATE` | 去除重复点。 |
| `W_TS_CONFLICT` | 同时间戳冲突，保留首值。 |
| `W_GAP` | 检测到超过阈值的缺口。 |
| `W_OFFSET_INSUFFICIENT` | 锚点不足/无锚点/退化，未应用时钟修正。 |
| `W_CLOCK_REORDER` | 时钟修正后出现逆序，已稳定排序。 |
| `W_CLOCK_DUPLICATE_TS` | 时钟修正后时间戳坍缩，保留首点。 |

## 11. 完整示例
```yaml
version: 1
name: basic
description: Five synthetic devices on an explicit one-second grid.
inputs:
  - id: dev-a1
    path: fixtures/series/dev-a1.csv
    format: csv
    device_id: dev-a1
    metric: temperature
  - id: dev-c3
    path: fixtures/series/dev-c3.jsonl
    format: jsonl
    device_id: dev-c3
    metric: temperature
anchors: fixtures/anchor/anchors.yaml
grid:
  origin_us: 1700000000000000
  step_us: 1000000
  end_us: 1700000060000000
max_gap_us: 2500000
monotonic_policy: sort_stable
duplicate_policy: keep_first
conflict_policy: keep_first
clock_skew: linear
resample:
  default: linear
  overrides:
    dev-c3: mean
gap_report_limit: 20
```

对应锚点文件：

```yaml
version: 1
reference_device: dev-a1
events:
  - id: evt-001
    observations:
      dev-a1: 1700000000000000
      dev-c3: 1700000000000000
  - id: evt-002
    observations:
      dev-a1: 1700000010000000
      dev-c3: 1700000010001000
  - id: evt-003
    observations:
      dev-a1: 1700000020000000
      dev-c3: 1700000020002000
```

此例中 `dev-c3` 的估计斜率约为 `100.0 µs/s`；三个事件满足最低锚点数量。
