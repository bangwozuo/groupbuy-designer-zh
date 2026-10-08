# -*- coding: utf-8 -*-
"""
团购定价测算 —— 确定性测算脚本（扣点 + 成本 + 毛利联动）。

职责边界（重要）：
  本脚本只做**确定性计算与产物落盘**：扣点/佣金/毛利/毛利率/经营毛利逐项计算、
  敏感性情景表、CSV/JSON/MD 报告生成。
  假设说明与定价策略建议由模型按 prompt.txt 完成。

确定性公式（与 prompt.txt 一致）：
  平台扣点 = 到手价 × 技术服务费率
  达人佣金 = 到手价 × 佣金率
  单份毛利 = 到手价 − 平台扣点 − 达人佣金 − 食材成本 − 包装成本
  单份经营毛利 = 单份毛利 − 固定分摊 − 人力分摊
  毛利率 = 单份毛利 ÷ 到手价 × 100%
  经营毛利率 = 单份经营毛利 ÷ 到手价 × 100%
  金额四舍五入到 0.1 元，比率保留 1 位小数

用法：
  python groupbuy_pricing_calculate.py --input ../../examples/input.json --outdir out
  python groupbuy_pricing_calculate.py --demo --outdir out

产物：
  out/测算明细.csv     逐项拆解（UTF-8 BOM）
  out/pricing.json    机器可读结果
  out/测算报告.md      结果 + 明细表 + 敏感性情景（佣金 ±2%、降价 10 元）
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys

# 内置 demo 数据：成都锦城巷串串香双人套餐真实测算（与 examples/input.json 一致）
DEMO = {
    "params": ("双人套餐抖音团购到手价89元；食材直接成本32元；包装与一次性餐具2.5元；"
               "房租水电等固定分摊8元/单；人力分摊12元/单；"
               "抖音平台技术服务费（到店餐饮类目）2%；达人佣金6%（按到手价计）"),
    "rules": ("公式：平台扣点=到手价×技术服务费率；达人佣金=到手价×佣金率；"
              "单份毛利=到手价−平台扣点−达人佣金−食材成本−包装成本；"
              "单份经营毛利=单份毛利−固定分摊−人力分摊；毛利率=单份毛利÷到手价。金额四舍五入到0.1元"),
}

NUM_RE = re.compile(
    r"(到手价|食材(?:直接)?成本|包装[^；;]*?|固定分摊|人力分摊|技术服务费|佣金率|佣金)[^；;。\d]*?(\d+(?:\.\d+)?)\s*(%|元)?")


def parse_params(text: str) -> dict:
    """从参数文本里抽数值（确定性抽取，缺失字段返回 None）。"""
    vals = {
        "price": None, "food": None, "pack": None, "fixed": None,
        "labor": None, "platform_rate": None, "commission_rate": None,
    }
    keymap = {"到手价": "price", "食材直接成本": "food", "食材成本": "food",
              "固定分摊": "fixed", "人力分摊": "labor",
              "技术服务费": "platform_rate", "佣金率": "commission_rate", "佣金": "commission_rate"}
    for m in NUM_RE.finditer(text):
        label, num, unit = m.group(1), float(m.group(2)), m.group(3)
        label = label.rstrip("：: （(等")
        key = None
        for k, v in keymap.items():
            if label.startswith(k):
                key = v
                break
        if key is None:
            if label.startswith("包装"):
                key = "pack"
            elif label.startswith("食材"):
                key = "food"
        if key is None:
            continue
        if key in ("price", "food", "pack", "fixed", "labor"):
            if unit in ("元", None) and vals[key] is None:
                vals[key] = num
        else:  # 费率
            if unit == "%" and vals[key] is None:
                vals[key] = num / 100.0
    return vals


def calc(p: dict, price_override: float | None = None) -> dict:
    price = price_override if price_override is not None else p["price"]
    deduct = round(price * p["platform_rate"], 1)
    commission = round(price * p["commission_rate"], 1)
    gross = round(price - deduct - commission - p["food"] - p["pack"], 1)
    gm_rate = round(gross / price * 100, 1)
    op = round(gross - p["fixed"] - p["labor"], 1)
    op_rate = round(op / price * 100, 1)
    return {"price": price, "deduct": deduct, "commission": commission, "gross": gross,
            "gm_rate": gm_rate, "op": op, "op_rate": op_rate}


def main() -> int:
    ap = argparse.ArgumentParser(description="团购定价测算：扣点+成本+毛利联动")
    ap.add_argument("--input", help="输入 JSON（含 params/rules）")
    ap.add_argument("--demo", action="store_true", help="使用内置演示数据")
    ap.add_argument("--outdir", default="out", help="产物输出目录")
    args = ap.parse_args()

    if args.demo:
        data = dict(DEMO)
    elif args.input:
        with open(args.input, encoding="utf-8") as f:
            data = json.load(f)
    else:
        print("[错误] 需要 --input 或 --demo", file=sys.stderr)
        return 2

    p = parse_params(str(data.get("params", "")))
    missing = [k for k, v in p.items() if v is None]
    if missing:
        print(f"[错误] 参数缺失：{missing}。请补充参数后重试，本脚本不做估算。",
              file=sys.stderr)
        return 2

    base = calc(p)
    # 敏感性情景：佣金 +2% / 到手价降 10 元
    s1 = calc(p | {"commission_rate": p["commission_rate"] + 0.02})
    s2 = calc(p, price_override=round(p["price"] - 10, 1))

    rows = [
        ("到手价", "给定", base["price"], "团购到手价（元/份）"),
        ("平台扣点", f'{base["price"]} × {p["platform_rate"]*100:g}%', base["deduct"], "技术服务费，按到手价计"),
        ("达人佣金", f'{base["price"]} × {p["commission_rate"]*100:g}%', base["commission"], "按到手价计"),
        ("食材成本", "给定", p["food"], "食材直接成本"),
        ("包装成本", "给定", p["pack"], "包装与一次性餐具"),
        ("单份毛利", "到手价−扣点−佣金−食材−包装", base["gross"], "已扣除平台扣点与佣金"),
        ("毛利率", f'{base["gross"]} ÷ {base["price"]}', f'{base["gm_rate"]}%', "毛利占到手价比例"),
        ("固定分摊", "给定", p["fixed"], "房租水电等摊到单份"),
        ("人力分摊", "给定", p["labor"], "人力成本摊到单份"),
        ("单份经营毛利", "毛利−固定分摊−人力分摊", base["op"], "经营层面利润（元/份）"),
        ("经营毛利率", f'{base["op"]} ÷ {base["price"]}', f'{base["op_rate"]}%', "经营毛利占到手价比例"),
    ]
    result = {
        "params": {k: round(v * 100, 2) if k.endswith("rate") else v for k, v in p.items()},
        "base": base, "rules": data.get("rules", ""),
        "sensitivity": [
            {"情景": "基准", **base},
            {"情景": "佣金率 +2 个百分点", **s1},
            {"情景": "到手价直降 10 元", **s2},
        ],
    }

    os.makedirs(args.outdir, exist_ok=True)
    csv_path = os.path.join(args.outdir, "测算明细.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["项目", "计算式", "结果", "说明"])
        w.writerows(rows)
    with open(os.path.join(args.outdir, "pricing.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    lines = [
        "# 团购定价测算报告", "",
        f"## 结果", "",
        f"到手价 **{base['price']} 元** 的套餐：单份毛利 **{base['gross']} 元**，"
        f"毛利率 **{base['gm_rate']}%**；单份经营毛利 **{base['op']} 元**（经营毛利率 {base['op_rate']}%）。", "",
        "## 测算明细", "",
        "| 项目 | 计算式 | 结果 | 说明 |", "|---|---|---|---|",
    ]
    lines += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows]
    lines += ["", "## 敏感性情景", "",
              "| 情景 | 到手价(元) | 单份毛利(元) | 毛利率 | 经营毛利(元) |", "|---|---|---|---|---|"]
    for s in result["sensitivity"]:
        lines.append(f"| {s['情景']} | {s['price']} | {s['gross']} | {s['gm_rate']}% | {s['op']} |")
    lines += ["", "> 本报告为 AI 生成内容（确定性脚本产出），涉及资金测算仅为草稿，需人工复核后再作决策。", ""]
    with open(os.path.join(args.outdir, "测算报告.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[完成] 毛利 {base['gross']} 元 / 毛利率 {base['gm_rate']}% → "
          f"{csv_path} / pricing.json / 测算报告.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
