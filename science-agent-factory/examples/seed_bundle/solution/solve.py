"""示例 Seed 的私有参考解法。

模块用途：读取示例测量表并生成任务卡要求的统计摘要；不负责验证其他任务或启动隔离环境。
主要接口：命令行 ``main``。
输入：相对当前工作目录的 ``input/measurements.csv``。
输出：相对当前工作目录的 ``output/summary.json``。
副作用与边界：创建 ``output`` 目录并覆盖目标 JSON；只使用 Python 标准库，不访问网络或外部进程。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path


def main() -> None:
    values: list[float] = []
    with Path("input/measurements.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            values.append(float(row["value"]))
    if not values:
        raise ValueError("measurements.csv must contain at least one value")
    summary = {
        "count": len(values),
        "mean": sum(values) / len(values),
        "minimum": min(values),
        "maximum": max(values),
    }
    output = Path("output/summary.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
