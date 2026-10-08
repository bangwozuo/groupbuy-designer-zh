# -*- coding: utf-8 -*-
"""
平台规则库 —— 确定性知识检索脚本（关键词召回 + 条目引用）。

职责边界（重要）：
  本脚本只做**确定性检索与产物落盘**：把用户粘贴的知识材料按【条目X】切分、
  提取 query 关键词、逐条打分召回（命中关键词数 ≥1 才召回）、按得分排序、
  CSV/JSON/MD 报告生成。规则解读与建议由模型按 prompt.txt 完成。

确定性规则（与 prompt.txt 一致）：
  R1 知识切分：按【条目X】标记切条，无标记按「；」粗切
  R2 召回门槛：单条命中 query 关键词数 ≥1 才进入结果；得分 = 命中数
  R3 排序：得分降序，同分按原文顺序
  R4 缺口：得分 0 的条目不计入引用；召回为空时如实输出「知识范围未覆盖」

用法：
  python platform_rules_library.py --input ../../examples/input.json --outdir out
  python platform_rules_library.py --demo --outdir out

产物：
  out/规则检索.csv    召回条目明细（条目号 / 得分 / 内容摘录）
  out/rules.json     机器可读结果
  out/规则检索报告.md  命中条目表 + 未命中条目清单
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys

STOPWORDS = {"多少", "什么", "怎么", "如何", "注意", "需要注意", "有什么", "哪些", "请问", "准备", "同时", "上架", "规则", "平台"}

ENTRY_RE = re.compile(r"【(条目[A-Z0-9]+)】([^【]+)")


def tokenize(q: str) -> list[str]:
    """极简确定性分词：先按标点切，再取 2-6 字片段做候选词。"""
    segs = re.split(r"[，。？！、；：（）“”\s?!,.;:()]+", q)
    words: list[str] = []
    for s in segs:
        if 2 <= len(s) <= 6 and s not in STOPWORDS:
            words.append(s)
        elif len(s) > 6:
            # 长段按 2-4 字滑窗取高频短词
            for n in (4, 3, 2):
                for i in range(0, len(s) - n + 1, n):
                    w = s[i:i + n]
                    if w not in STOPWORDS and w not in words:
                        words.append(w)
    return words[:24]


def split_entries(knowledge: str) -> list[dict]:
    entries = [{"id": m.group(1), "text": m.group(2).strip().rstrip("。")}
               for m in ENTRY_RE.finditer(knowledge)]
    if not entries:  # R1 无标记回退
        entries = [{"id": f"条目{i+1}", "text": t.strip()}
                   for i, t in enumerate(knowledge.split("；")) if t.strip()]
    return entries


def main() -> int:
    ap = argparse.ArgumentParser(description="平台规则库：确定性知识检索")
    ap.add_argument("--input", help="输入 JSON（含 query/knowledge）")
    ap.add_argument("--demo", action="store_true", help="使用内置演示数据")
    ap.add_argument("--outdir", default="out", help="产物输出目录")
    args = ap.parse_args()

    DEMO = json.load(open(os.path.join(os.path.dirname(HERE := os.path.dirname(
        os.path.abspath(__file__))), "examples", "input.json"), encoding="utf-8"))

    if args.demo:
        data = DEMO
    elif args.input:
        with open(args.input, encoding="utf-8") as f:
            data = json.load(f)
    else:
        print("[错误] 需要 --input 或 --demo", file=sys.stderr)
        return 2

    query = str(data.get("query", "")).strip()
    knowledge = str(data.get("knowledge", "")).strip()
    if not query or not knowledge:
        print("[错误] query 与 knowledge 均为必填；知识范围缺失时本脚本不作答，避免靠常识编造",
              file=sys.stderr)
        return 2

    words = tokenize(query)
    entries = split_entries(knowledge)
    scored = []
    for e in entries:
        score = sum(1 for w in words if w in e["text"])
        e["score"] = score
        if score >= 1:  # R2 召回门槛
            scored.append(e)
    scored.sort(key=lambda x: (-x["score"],))  # R3 稳定排序（同分保持原顺序）
    missed = [e for e in entries if e["score"] == 0]

    result = {
        "query": query,
        "keywords": words,
        "citations": [f"{e['id']}（命中 {e['score']} 个关键词）：{e['text'][:80]}" for e in scored],
        "answer": None,  # 语境整合由模型完成；脚本只提供召回与引用
        "gaps": [f"未命中条目：{e['id']}" for e in missed],
        "summary": {"条目总数": len(entries), "召回条数": len(scored),
                    "未命中条数": len(missed), "召回率": f"{round(len(scored)/len(entries)*100,1)}%"},
    }

    os.makedirs(args.outdir, exist_ok=True)
    csv_path = os.path.join(args.outdir, "规则检索.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["条目号", "得分", "内容"])
        for e in scored:
            w.writerow([e["id"], e["score"], e["text"]])
    with open(os.path.join(args.outdir, "rules.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    lines = [
        "# 平台规则检索报告", "",
        f"- 检索问题：{query}",
        f"- 关键词：{'、'.join(words) if words else '（无）'}",
        f"- 条目总数 {len(entries)}，召回 {len(scored)} 条（召回率 {result['summary']['召回率']}）", "",
        "| 条目号 | 得分 | 内容 |", "|---|---|---|",
    ]
    lines += [f"| {e['id']} | {e['score']} | {e['text'][:60]} |" for e in scored]
    if missed:
        lines += ["", "## 未命中条目（不计入引用）", ""]
        lines += [f"- {e['id']}：{e['text'][:60]}"]
    lines += ["", "> 本报告为 AI 生成内容（确定性脚本产出）；平台规则可能更新，正式上架前需人工在商家后台核实。", ""]
    with open(os.path.join(args.outdir, "规则检索报告.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[完成] 召回 {len(scored)}/{len(entries)} 条 → {csv_path} / rules.json / 规则检索报告.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
