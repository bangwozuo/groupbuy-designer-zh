# -*- coding: utf-8 -*-
"""
竞品团购追踪工作流 —— 端到端编排脚本（对齐 SKILL.md 步骤链路）。

流程（与 SKILL.md 的 DAG 一致）：
  S1 竞品团购采集（确定性解析，对齐原子技能 competitor-groupbuy-collect 的规则）
  S2 追踪对比汇总（本流脚本：价格带分布 / 折扣率排行 / 追踪要点）
  S3 人工确认（不在脚本内）

本脚本承担 S1/S2 的确定性部分：手摘记录解析（一条一个带圈序号）、
折扣率 = 现价 ÷ 原价 × 100%（保留 1 位小数）、价格带 <100 元引流带 /
100-200 元主力带 / >200 元利润带、条数上限 10 条超出按现价升序截断。

用法：
  python run_flow.py --input input.json --outdir out
  python run_flow.py --demo

产物：
  out/step1_采集明细.json   S1 结构化条目
  out/flow_result.json     机器可读结果（summary/steps/deliverable）
  out/追踪对比报告.md       最终交付物（对比表）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WF_DIR = os.path.dirname(HERE)
SKILL_SCRIPT = os.path.normpath(os.path.join(
    WF_DIR, "..", "..", "skills", "competitor-groupbuy-collect",
    "scripts", "competitor_groupbuy_collect.py"))

MARK_RE = re.compile(r"[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]")
ORIG_RE = re.compile(r"原价\s*(\d+(?:\.\d+)?)\s*元")
NOW_RE = re.compile(r"现价\s*(\d+(?:\.\d+)?)\s*元")
MAX_ITEMS = 10


def parse(source: str) -> list[dict]:
    items = []
    for raw in MARK_RE.split(source)[1:]:
        raw = raw.strip().rstrip("；;。").strip()
        if not raw:
            continue
        segs = raw.split("·")
        m_o, m_n = ORIG_RE.search(raw), NOW_RE.search(raw)
        now = float(m_n.group(1)) if m_n else None
        orig = float(m_o.group(1)) if m_o else None
        items.append({
            "标题": segs[0].strip(),
            "套餐类型": segs[1].strip() if len(segs) > 1 else "",
            "原价_元": orig, "现价_元": now,
            "折扣率_%": round(now / orig * 100, 1) if (now and orig) else None,
            "内容与限制": "·".join(segs[2:]).strip() if len(segs) > 2 else "",
        })
    return items


def band(p):
    if p is None:
        return "未定价"
    return "引流带(<100元)" if p < 100 else ("主力带(100-200元)" if p <= 200 else "利润带(>200元)")


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

    raw = str(data.get("input", data.get("source", "")))
    if not raw.strip():
        print("[错误] 输入为空：本工作流只处理真实摘录，不虚构条目", file=sys.stderr)
        return 2

    # S1 竞品团购采集（确定性解析，与原子技能脚本同规则）
    items = parse(raw)
    if len(items) > MAX_ITEMS:
        items = sorted(items, key=lambda x: x["现价_元"] or 9e9)[:MAX_ITEMS]
    priced = [i for i in items if i["现价_元"] is not None]
    bands: dict[str, int] = {}
    for i in priced:
        b = band(i["现价_元"])
        i["价格带"] = b
        bands[b] = bands.get(b, 0) + 1
    discounts = [i["折扣率_%"] for i in items if i["折扣率_%"] is not None]

    os.makedirs(args.outdir, exist_ok=True)
    step1_path = os.path.join(args.outdir, "step1_采集明细.json")
    with open(step1_path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    # S2 追踪对比汇总（读上一步产物）
    with open(step1_path, encoding="utf-8") as f:
        s1_items = json.load(f)
    deep = [i for i in s1_items if i["折扣率_%"] is not None and i["折扣率_%"] < 50]

    report = [
        "# 竞品团购追踪对比报告", "",
        f"- 采集条数：{len(s1_items)} 条（上限 {MAX_ITEMS} 条）",
        (f"- 现价区间：{min(i['现价_元'] for i in priced)} - {max(i['现价_元'] for i in priced)} 元"
         if priced else "- 现价区间：-"),
        f"- 价格带分布：{'；'.join(f'{k} {v} 条' for k, v in bands.items()) or '无'}",
        f"- 深折扣条目（折扣率<50%）：{len(deep)} 条", "",
        "| # | 店名/套餐 | 原价(元) | 现价(元) | 折扣率 | 价格带 | 追踪要点 |",
        "|---|---|---|---|---|---|---|",
    ]
    for n, i in enumerate(s1_items, 1):
        point = ("深折扣引流，需重点盯" if i["折扣率_%"] and i["折扣率_%"] < 50
                 else (i["内容与限制"] or "常规在售")[:30])
        orig = i["原价_元"] if i["原价_元"] is not None else "-"
        lines = (i["标题"], i["套餐类型"])
        report.append(f"| {n} | {lines[0]}·{lines[1]} | {orig} | {i['现价_元']} | "
                      f"{i['折扣率_%'] if i['折扣率_%'] is not None else '-'}% | {i.get('价格带','-')} | {point} |")
    report += ["", "> 本报告为 AI 生成内容，发布或决策前需人工复核（S3 人工确认）。", ""]

    result = {
        "summary": f"采集竞品团购 {len(s1_items)} 条，现价区间 "
                   f"{min(i['现价_元'] for i in priced) if priced else '-'} - "
                   f"{max(i['现价_元'] for i in priced) if priced else '-'} 元，深折扣 {len(deep)} 条",
        "steps": {"S1_竞品团购采集": f"解析 {len(s1_items)} 条 → {os.path.basename(step1_path)}",
                  "S2_追踪对比汇总": "价格带分布 + 折扣率排行 + 追踪要点表",
                  "S3_人工确认": "交付前人工复核"},
        "deliverable": "\n".join(report),
    }
    with open(os.path.join(args.outdir, "flow_result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.outdir, "追踪对比报告.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(report))

    # 复用原子技能脚本产出 CSV 三件套（保持口径一致；脚本缺失不阻断主流程）
    if os.path.exists(SKILL_SCRIPT):
        import subprocess
        subprocess.run([sys.executable, SKILL_SCRIPT, "--demo", "--outdir",
                        os.path.join(args.outdir, "skill_artifacts")], check=False)

    print(f"[完成] S1 解析 {len(s1_items)} 条 → {args.outdir}/追踪对比报告.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
