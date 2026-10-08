# -*- coding: utf-8 -*-
"""
核销数据复盘调价工作流 —— 端到端编排脚本（对齐 SKILL.md 步骤链路）。

流程（与 SKILL.md 的 DAG 一致）：
  S1 核销数据结构化（本流脚本：逐档解析 售出/核销/核销率 与成本参数）
  S2 分档毛利测算（脚本承担：调原子技能 groupbuy-pricing-calculate）
  S3 滞销识别与调价建议（本流脚本：核销率 <60% 判滞销；滞销款按降价 10% 重测毛利，
     调价后经营毛利 ≤0 → 建议「换品而非降价」；核销率 ≥80% 标「表现良好」）
  S4 人工确认（不在脚本内）

确定性规则（与 prompt.txt 一致）：
  核销率 = 核销份数 ÷ 售出份数 × 100%（以输入实测为准，重算交叉核对）
  滞销线 60%；表现线 80%；降价幅度 10%；金额四舍五入到 0.1 元

用法：
  python run_flow.py --input input.json --outdir out
  python run_flow.py --demo
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WF_DIR = os.path.dirname(HERE)
PRICING_SCRIPT = os.path.normpath(os.path.join(
    WF_DIR, "..", "..", "skills", "groupbuy-pricing-calculate",
    "scripts", "groupbuy_pricing_calculate.py"))

TIER_RE = re.compile(r"[①②③④⑤]([^①②③④⑤]+)")
SALE_RE = re.compile(r"(.{2,20}?)（到手价(\d+(?:\.\d+)?)元）售出(\d+)份，核销(\d+)份，核销率(\d+(?:\.\d+)?)%")
COST_RE = re.compile(r"([\u4e00-\u9fa5]{2,10}?)食材(\d+(?:\.\d+)?)元\+?包装(\d+(?:\.\d+)?)元")
RATE_RE = re.compile(r"技术服务费(\d+(?:\.\d+)?)%")
COMM_RE = re.compile(r"佣金(\d+(?:\.\d+)?)%")
FIXED_RE = re.compile(r"固定分摊(\d+(?:\.\d+)?)元")
LABOR_RE = re.compile(r"人力分摊(\d+(?:\.\d+)?)元")
DEADLINE = 60.0   # 滞销线（%）
GOODLINE = 80.0   # 表现线（%）
CUT = 10.0        # 滞销款建议降价幅度（%）


def parse(text: str) -> tuple[list[dict], dict, str]:
    """S1：核销数据 + 成本参数 + 调整目标 确定性解析。"""
    parts = re.split(r"[一二三]、", text)
    sales_text = next((p for p in parts if "售出" in p), "")
    cost_text = next((p for p in parts if "食材" in p), "")
    goal_text = next((p for p in parts if "目标" in p or "滞销" in p), "")

    tiers = []
    for seg in TIER_RE.split(sales_text)[1:]:
        m = SALE_RE.search(seg)
        if not m:
            continue
        name, price, sold, used, rate = (m.group(1).strip(), float(m.group(2)),
                                         int(m.group(3)), int(m.group(4)),
                                         float(m.group(5)))
        real_rate = round(used / sold * 100, 1)  # 交叉核对
        tiers.append({"名称": name, "price": price, "sold": sold, "used": used,
                      "rate": rate, "rate_recalc": real_rate,
                      "rate_match": abs(real_rate - rate) < 0.1})

    costs = {}
    for m in COST_RE.finditer(cost_text):
        costs[m.group(1).strip()] = (float(m.group(2)), float(m.group(3)))
    g = {
        "platform_rate": (float(RATE_RE.search(cost_text).group(1)) / 100
                          if RATE_RE.search(cost_text) else None),
        "commission_rate": (float(COMM_RE.search(cost_text).group(1)) / 100
                            if COMM_RE.search(cost_text) else None),
        "fixed": (float(FIXED_RE.search(cost_text).group(1))
                  if FIXED_RE.search(cost_text) else None),
        "labor": (float(LABOR_RE.search(cost_text).group(1))
                  if LABOR_RE.search(cost_text) else None),
    }
    return tiers, costs, g, goal_text


def match_cost(name: str, costs: dict) -> tuple[float, float] | None:
    for k, v in costs.items():
        if k[:4] in name or name[:4] in k:
            return v
    return None


def run_pricing(name: str, price: float, food: float, pack: float, g: dict,
                outdir: str) -> dict | None:
    if not os.path.exists(PRICING_SCRIPT):
        return None
    params = (f"{name}到手价{price}元；食材直接成本{food}元；包装与一次性餐具{pack}元；"
              f"房租水电等固定分摊{g['fixed']}元/单；人力分摊{g['labor']}元/单；"
              f"平台技术服务费{round(g['platform_rate']*100, 2):g}%；"
              f"达人佣金{round(g['commission_rate']*100, 2):g}%（按到手价计）")
    with tempfile.TemporaryDirectory() as td:
        ip = os.path.join(td, "in.json")
        with open(ip, "w", encoding="utf-8") as f:
            json.dump({"params": params, "rules": ""}, f, ensure_ascii=False)
        r = subprocess.run([sys.executable, PRICING_SCRIPT, "--input", ip,
                            "--outdir", outdir], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[警告] S2 对「{name}」测算失败：{r.stderr.strip()[:120]}", file=sys.stderr)
            return None
        with open(os.path.join(outdir, "pricing.json"), encoding="utf-8") as f:
            return json.load(f)["base"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="输入 JSON（含 input 字段）")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--outdir", default="out")
    args = ap.parse_args()

    if args.demo:
        with open(os.path.join(WF_DIR, "examples", "input.json"), encoding="utf-8") as f:
            data = json.load(f)
    elif args.input:
        with open(args.input, encoding="utf-8") as f:
            data = json.load(f)
    else:
        print("[错误] 需要 --input 或 --demo", file=sys.stderr)
        return 2

    raw = str(data.get("input", ""))
    if not raw.strip():
        print("[错误] 输入为空", file=sys.stderr)
        return 2

    # S1 结构化
    tiers, costs, g, goal = parse(raw)
    if not tiers:
        print("[错误] 未解析到核销数据（需「① 名称（到手价X元）售出N份，核销M份，核销率P%」格式）",
              file=sys.stderr)
        return 2
    missing = [k for k, v in g.items() if v is None]
    if missing:
        print(f"[错误] 成本参数缺失：{missing}，不估算", file=sys.stderr)
        return 2
    os.makedirs(args.outdir, exist_ok=True)
    step1_path = os.path.join(args.outdir, "step1_核销结构化.json")
    with open(step1_path, "w", encoding="utf-8") as f:
        json.dump({"tiers": tiers, "costs": {k: list(v) for k, v in costs.items()},
                   "global": g, "goal": goal}, f, ensure_ascii=False, indent=2)

    # S2 分档毛利测算（读上一步产物）
    with open(step1_path, encoding="utf-8") as f:
        s1 = json.load(f)
    rows = []
    for t in s1["tiers"]:
        c = match_cost(t["名称"], s1["costs"])
        if c is None:
            print(f"[警告] 「{t['名称']}」无对应成本参数，跳过测算", file=sys.stderr)
            continue
        base = run_pricing(t["名称"], t["price"], c[0], c[1], s1["global"],
                           os.path.join(args.outdir, "step2_毛利测算"))
        if not base:
            continue
        row = {"名称": t["名称"], "price": t["price"], "rate": t["rate"],
               "rate_match": t["rate_match"], "sold": t["sold"], "used": t["used"], **base}
        # S3 滞销识别与调价建议
        if t["rate"] < DEADLINE:
            new_price = round(t["price"] * (1 - CUT / 100), 1)
            after = run_pricing(t["名称"], new_price, c[0], c[1], s1["global"],
                                os.path.join(args.outdir, "step3_调价测算"))
            if after and after["op"] <= 0:
                row["判定"] = "滞销"
                row["建议"] = (f"核销率 {t['rate']}% <60% 判滞销；降价 {CUT:g}%（至 {new_price} 元）"
                              f"后经营毛利 {after['op']} 元 ≤0，降价无效 → 建议换品")
            elif after:
                row["判定"] = "滞销"
                row["建议"] = (f"核销率 {t['rate']}% <60% 判滞销；建议降价 {CUT:g}%（至 {new_price} 元），"
                              f"调价后单份毛利 {after['gross']} 元、经营毛利率 {after['op_rate']}%")
            else:
                row["判定"], row["建议"] = "滞销", f"核销率 {t['rate']}% <60% 判滞销（调价测算失败，需人工评估）"
        elif t["rate"] >= GOODLINE:
            row["判定"], row["建议"] = "表现良好", f"核销率 {t['rate']}% ≥80%，维持现价，可考虑作为组合锚品"
        else:
            row["判定"], row["建议"] = "正常", f"核销率 {t['rate']}% 介于 60-80%，观察 1 个周期再决策"
        rows.append(row)

    # 汇总
    n_bad = sum(1 for r in rows if r["判定"] == "滞销")
    report = [
        "# 核销数据复盘调价报告", "",
        f"- 复盘周期：2026-09（以输入为准）；滞销线 {DEADLINE:g}%，表现线 {GOODLINE:g}%，建议降幅 {CUT:g}%",
        f"- 共 {len(rows)} 档：滞销 {n_bad} 档", "",
        "| 套餐 | 到手价(元) | 核销率 | 判定 | 单份毛利(元) | 经营毛利率 | 调价/换品建议 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        flag = "" if r["rate_match"] else "（输入核销率与重算不一致，已按重算口径）"
        report.append(f"| {r['名称']}{flag} | {r['price']} | {r['rate']}% | {r['判定']} | "
                      f"{r['gross']} | {r['op_rate']}% | {r['建议']} |")
    report += ["", "> 本报告为 AI 生成内容（确定性脚本产出），调价决策前需人工复核（S4）。", ""]

    result = {
        "summary": f"复盘 {len(rows)} 档，滞销 {n_bad} 档，已给出逐档调价/换品建议",
        "steps": {"S1_核销结构化": f"{len(tiers)} 档 → {os.path.basename(step1_path)}",
                  "S2_分档毛利测算": "调 groupbuy-pricing-calculate 逐档测算",
                  "S3_滞销识别与调价": f"<{DEADLINE:g}% 判滞销，降幅 {CUT:g}% 重测",
                  "S4_人工确认": "交付前人工复核"},
        "deliverable": "\n".join(report),
    }
    with open(os.path.join(args.outdir, "flow_result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.outdir, "复盘调价报告.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(report))

    print(f"[完成] {len(rows)} 档复盘，滞销 {n_bad} 档 → {args.outdir}/复盘调价报告.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
