# -*- coding: utf-8 -*-
"""
套餐结构设计工作流 —— 端到端编排脚本（对齐 SKILL.md 步骤链路）。

流程（与 SKILL.md 的 DAG 一致）：
  S1 套餐参数解析（本流脚本：引流款/主力款/利润款三档参数逐档抽取）
  S2 分档毛利测算（脚本承担：调原子技能 groupbuy-pricing-calculate 逐档测算）
  S3 三级组合汇总（本流脚本：组合对比表 + 毛利红线提示）
  S4 人工确认（不在脚本内）

确定性规则（与 prompt.txt 一致）：
  三档定价 = 引流款 / 主力款 / 利润款；金额四舍五入到 0.1 元
  毛利公式与原子技能一致；经营毛利率 <25% 提示「需复核定价」，<15% 建议重新设计

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
PRICE_RE = re.compile(r"到手价\s*(\d+(?:\.\d+)?)\s*元")
FOOD_RE = re.compile(r"食材成本\s*(\d+(?:\.\d+)?)\s*元")
PACK_RE = re.compile(r"包装[^，。；;]*?(\d+(?:\.\d+)?)\s*元")
TIER_NAME_RE = re.compile(r"(引流款|主力款|利润款)[·・:]?(.*?)(?:到手价)")
RATE_RE = re.compile(r"(?:技术服务费|扣点)\D*?(\d+(?:\.\d+)?)\s*%")
COMM_RE = re.compile(r"佣金\D*?(\d+(?:\.\d+)?)\s*%")
FIXED_RE = re.compile(r"固定分摊按?\s*(\d+(?:\.\d+)?)\s*元")
LABOR_RE = re.compile(r"人力分摊按?\s*(\d+(?:\.\d+)?)\s*元")


def parse_tiers(text: str) -> tuple[list[dict], dict]:
    """S1：逐档抽取三档参数 + 全局费率（确定性，缺失即报错不估算）。"""
    rates = RATE_RE.search(text)
    comm = COMM_RE.search(text)
    fixed = FIXED_RE.search(text)
    labor = LABOR_RE.search(text)
    g = {
        "platform_rate": float(rates.group(1)) / 100 if rates else None,
        "commission_rate": float(comm.group(1)) / 100 if comm else None,
        "fixed": float(fixed.group(1)) if fixed else None,
        "labor": float(labor.group(1)) if labor else None,
    }
    tiers = []
    for seg in TIER_RE.split(text)[1:]:
        name = TIER_NAME_RE.search(seg)
        price = PRICE_RE.search(seg)
        food = FOOD_RE.search(seg)
        pack = PACK_RE.search(seg)
        if not (name and price and food):
            continue
        tiers.append({
            "档位": name.group(1),
            "套餐名": name.group(2).strip("，。；;、 "),
            "price": float(price.group(1)),
            "food": float(food.group(1)),
            "pack": float(pack.group(1)) if pack else 0.0,
        })
    return tiers, g


def run_pricing(tier: dict, g: dict, outdir: str) -> dict | None:
    """S2：调原子技能定价脚本逐档测算（每步读上一步产物）。"""
    if not os.path.exists(PRICING_SCRIPT):
        return None
    params = (f"{tier['套餐名']}到手价{tier['price']}元；食材直接成本{tier['food']}元；"
              f"包装与一次性餐具{tier['pack']}元；房租水电等固定分摊{g['fixed']}元/单；"
              f"人力分摊{g['labor']}元/单；平台技术服务费{round(g['platform_rate']*100, 2):g}%；"
              f"达人佣金{round(g['commission_rate']*100, 2):g}%（按到手价计）")
    with tempfile.TemporaryDirectory() as td:
        ip = os.path.join(td, "in.json")
        with open(ip, "w", encoding="utf-8") as f:
            json.dump({"params": params, "rules": ""}, f, ensure_ascii=False)
        r = subprocess.run([sys.executable, PRICING_SCRIPT, "--input", ip,
                            "--outdir", outdir], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[警告] S2 定价脚本对「{tier['档位']}」失败：{r.stderr.strip()[:120]}",
                  file=sys.stderr)
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
        print("[错误] 输入为空：三档参数缺失时本工作流不估算", file=sys.stderr)
        return 2

    # S1 参数解析
    tiers, g = parse_tiers(raw)
    missing_g = [k for k, v in g.items() if v is None]
    if not tiers or missing_g:
        print(f"[错误] 参数解析不足：档数={len(tiers)}，全局缺失={missing_g}。"
              f"请按「① 引流款·套餐名到手价X元，食材成本Y元，包装Z元；②…」格式补齐",
              file=sys.stderr)
        return 2
    os.makedirs(args.outdir, exist_ok=True)
    step1 = {"tiers": tiers, "global": g}
    step1_path = os.path.join(args.outdir, "step1_三档参数.json")
    with open(step1_path, "w", encoding="utf-8") as f:
        json.dump(step1, f, ensure_ascii=False, indent=2)

    # S2 分档毛利测算（读 step1 产物）
    with open(step1_path, encoding="utf-8") as f:
        s1 = json.load(f)
    rows = []
    for t in s1["tiers"]:
        base = run_pricing(t, s1["global"], args.outdir)
        if base:
            rows.append({"档位": t["档位"], "套餐名": t["套餐名"], **base})
    if not rows:
        print("[错误] S2 全部档位测算失败，中止", file=sys.stderr)
        return 2

    # S3 三级组合汇总
    warn = []
    for r in rows:
        if r["op_rate"] < 15:
            warn.append(f"{r['档位']}经营毛利率 {r['op_rate']}% <15%，建议重新设计套餐")
        elif r["op_rate"] < 25:
            warn.append(f"{r['档位']}经营毛利率 {r['op_rate']}% <25%，需复核定价")

    report = [
        "# 套餐三级组合测算报告", "",
        f"- 全局费率：技术服务费 {round(s1['global']['platform_rate']*100,2):g}%，"
        f"达人佣金 {round(s1['global']['commission_rate']*100,2):g}%，"
        f"固定分摊 {s1['global']['fixed']} 元/单，人力分摊 {s1['global']['labor']} 元/单", "",
        "| 档位 | 套餐 | 到手价(元) | 单份毛利(元) | 毛利率 | 经营毛利(元) | 经营毛利率 | 红线判定 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        verdict = ("🔴 <15% 重新设计" if r["op_rate"] < 15
                   else ("🟡 <25% 需复核" if r["op_rate"] < 25 else "🟢 达标"))
        report.append(f"| {r['档位']} | {r['套餐名']} | {r['price']} | {r['gross']} | "
                      f"{r['gm_rate']}% | {r['op']} | {r['op_rate']}% | {verdict} |")
    if warn:
        report += ["", "## 红线提示", ""] + [f"- {w}" for w in warn]
    report += ["", "## 组合建议（基于测算的确定性提示）", "",
               f"- 引流款定价重心是拉新，毛利可薄；主力款经营毛利率建议 ≥25% 保经营盘；"
               f"利润款承担利润，若 <25% 优先调结构（降食材成本或提价）而非砍赠品。",
               "- 组合价差：引流款与主力款到手价建议拉开 ≥2 倍，制造价格锚点。", "",
               "> 本报告为 AI 生成内容（确定性脚本产出），定价决策前需人工复核（S4）。", ""]

    tier_brief = "；".join(f"{r['档位']}毛利率{r['gm_rate']}%" for r in rows)
    result = {
        "summary": f"三档组合测算完成：{tier_brief}",
        "steps": {"S1_参数解析": f"{len(s1['tiers'])} 档 → {os.path.basename(step1_path)}",
                  "S2_分档毛利测算": "调 groupbuy-pricing-calculate 逐档测算",
                  "S3_三级组合汇总": "组合对比表 + 毛利红线提示",
                  "S4_人工确认": "交付前人工复核"},
        "deliverable": "\n".join(report),
    }
    with open(os.path.join(args.outdir, "flow_result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.outdir, "三级组合测算报告.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(report))

    print(f"[完成] {len(rows)} 档测算 → {args.outdir}/三级组合测算报告.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
