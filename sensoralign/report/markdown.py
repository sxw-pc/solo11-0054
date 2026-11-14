"""Deterministic Markdown rendering of the JSON report object."""

from __future__ import annotations

import json
from typing import Any

from ..core.resample import MARKER_ORDER


def render_markdown(report: dict[str, Any]) -> str:
    """Render a complete Markdown report with stable table ordering."""
    contract = report["contract"]
    snapshot = contract["snapshot"]
    grid = report["grid"]
    alignment = report["alignment"]
    limit = int(snapshot.get("gap_report_limit", 20))
    lines: list[str] = [
        "# SensorAlign 对齐报告",
        "",
        "## 项目摘要",
        "",
        f"- 契约：`{contract['name']}` (version {contract['version']})",
        f"- 契约指纹：`{contract['sha256']}`",
        f"- 网格：{grid['mode']}，origin `{grid['origin_us']}` µs，step `{grid['step_us']}` µs，"
        f"end `{grid['end_us']}` µs",
        f"- 网格格数：{grid['cell_count']}",
        f"- 系列数：{len(report['series'])}",
        f"- 公共有值格：{alignment['common_cells']}；任一路缺失：{alignment['missing_any_cells']}",
        f"- 对齐完成率：{_number(alignment['completion_rate'])}",
        "",
        "## 契约快照",
        "",
        "```json",
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2),
        "```",
        "",
        "## 每路质量",
        "",
    ]
    rows = []
    for item in report["series"]:
        quality = item["quality"]
        markers = ", ".join(f"{key}={quality['marker_counts'].get(key, 0)}" for key in MARKER_ORDER)
        rows.append(
            [
                item["series_id"],
                item["device_id"],
                item["metric"],
                item["strategy"],
                _number(quality["coverage"]),
                _number(quality["gap_ratio"]),
                str(quality["conflict_count"]),
                str(quality["gap_count"]),
                markers,
            ]
        )
    lines.extend(
        _table(
            ["系列", "设备", "物理量", "策略", "覆盖率", "缺口格占比", "冲突", "缺口数", "标记分布"],
            rows,
        )
    )
    lines.extend(["", "## 时钟偏移估计", ""])
    offset_rows = []
    for estimate in report["offsets"]:
        offset_rows.append(
            [
                estimate["device_id"],
                estimate["reference_device"] or "-",
                estimate["status"],
                str(estimate["anchor_count"]),
                _optional_int(estimate["median_offset_us"]),
                _optional_number(estimate["slope_us_per_s"]),
                _optional_number(estimate["intercept_us"]),
                _optional_number(estimate["residual_median_us"]),
            ]
        )
    lines.extend(
        _table(
            ["设备", "参考设备", "状态", "锚点数", "中位数偏移 µs", "斜率 µs/s", "截距 µs", "残差中位数 µs"],
            offset_rows,
        )
    )
    lines.extend(["", "## 缺口清单", ""])
    gaps = report["gaps"]
    if not gaps:
        lines.append("无。")
    else:
        gap_rows = [
            [gap["series_id"], str(gap["start_us"]), str(gap["end_us"]), str(gap["duration_us"])]
            for gap in gaps[:limit]
        ]
        lines.extend(_table(["系列", "左侧 µs", "右侧 µs", "时长 µs"], gap_rows))
        if len(gaps) > limit:
            lines.append("")
            lines.append(f"仅展示前 {limit} 条，共 {len(gaps)} 条。")
    lines.extend(["", "## 事件与错误清单", ""])
    errors = report["errors"]
    if not errors:
        lines.append("无。")
    else:
        error_rows = [
            [
                str(event["seq"]),
                event["severity"],
                event["code"],
                event["series_id"] or "-",
                event["message"],
            ]
            for event in errors[:limit]
        ]
        lines.extend(_table(["#", "级别", "代码", "系列", "说明"], error_rows))
        if len(errors) > limit:
            lines.append("")
            lines.append(f"仅展示前 {limit} 条，共 {len(errors)} 条。")
    lines.extend(
        [
            "",
            "## 说明",
            "",
            "- 时间戳均为整数微秒；缺口区间的格标记为 `in_gap`，未被静默填充。",
            "- `linear` 需要左右两侧观测；`step` 取不晚于网格时刻的最近值；"
            "`mean` 使用以网格点为中心、宽度为一个网格步长的窗口；"
            "`lag_window` 使用右端对齐网格时刻的固定滞后窗口 `[t - step, t)`。",
            "- 本报告不含运行时间戳，输入与契约不变时输出字节级稳定。",
            "",
        ]
    )
    return "\n".join(lines)


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(_escape(cell) for cell in row) + " |")
    return lines


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _number(value: float) -> str:
    return f"{value:.6f}"


def _optional_number(value: float | None) -> str:
    return "-" if value is None else _number(value)


def _optional_int(value: int | None) -> str:
    return "-" if value is None else str(value)
