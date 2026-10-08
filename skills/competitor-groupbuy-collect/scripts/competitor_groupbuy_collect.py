# -*- coding: utf-8 -*-
"""
竞品团购采集 —— 确定性解析与对比统计脚本。

职责边界（重要）：
  本脚本只做**确定性计算与产物落盘**：解析用户手动摘录的竞品团购原始记录、
  折扣率/价格带统计、TOP 低价排序、CSV/JSON/MD 报告生成。
  语境解读、竞争策略建议由模型按 prompt.txt 完成。

确定性规则（与 prompt.txt 一致）：
  R1 折扣率 = 现价 ÷ 原价 × 100%（无原价的条目不计算，标「-」）
  R2 条数上限：默认最多输出 10 条，超出按「现价从低到高」截断
  R3 价格带：按现价划 3 档 —— <100 元（引流带）/ 100-200 元（主力带）/ >200 元（利润带）
  R4 全部金额为团购到手价（元），四舍五入到 0.1 元

用法：
  python competitor_groupbuy_collect.py --input ../../examples/input.json --outdir out
  python competitor_groupbuy_collect.py --demo --outdir out

产物：
  out/采集明细.csv    逐条结构化明细（UTF-8 BOM，Excel 可直接打开）
  out/collect.json    机器可读结果（供工作流/下游技能读取）
  out/采集对比报告.md  价格带分布 + 折扣率排行 + 摘要
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys

MAX_ITEMS = 10  # R2 条数上限

# 内置 demo 数据：武汉江汉路商圈火锅品类 6 条真实摘录（与 examples/input.json 一致）
DEMO = {
    "source": (
        "美团App手动截图与手动摘录（武汉江汉路商圈·火锅品类，2026-09-25至2026-10-07），"
        "用户粘贴的原始记录：① 蜀大侠火锅(江汉路店)·双人套餐·原价268元·现价98元·"
        "含锅底/8荤4素/两杯饮料；② 巴奴毛肚火锅(中山大道店)·2-3人毛肚套餐·现价219元·"
        "含招牌毛肚/虾滑/共12个菜品；③ 佩姐老火锅(江汉路店)·四人畅吃套餐·现价328元·"
        "含12荤8素/锅底任选；④ 电台巷火锅(循礼门店)·双人餐·现价128元·"
        "含锅底/6荤4素/小吃拼盘；⑤ 海底捞(佳丽广场店)·工作日午市双人套餐·现价158元·"
        "限周一至周五11:00-14:00使用；⑥ 老码头火锅(武汉国际广场店)·3-4人套餐·现价268元·"
        "赠毛肚1份/饮料3杯"
    ),
    "scope": "时间窗口：2026-09-25至2026-10-07；条数上限：10条；筛选条件：仅火锅品类、距商圈中心3公里内、上架状态为在售",
}

MARK_RE = re.compile(r"[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]")
ORIG_RE = re.compile(r"原价\s*(\d+(?:\.\d+)?)\s*元")
NOW_RE = re.compile(r"现价\s*(\d+(?:\.\d+)?)\s*元")
NAME_SPLIT = re.compile(r"[·：:]")


def parse_records(source: str) -> list[dict]:
    """把手摘记录拆成结构化条目（确定性解析，不做任何推断补全）。"""
    parts = MARK_RE.split(source)
    items: list[dict] = []
    for raw in parts[1:]:
        raw = raw.strip().rstrip("；;。").strip()
        if not raw:
            continue
        title = raw.split("·")[0].strip()
        segs = raw.split("·")
        combo = segs[1].strip() if len(segs) > 1 else ""
        extra = "·".join(segs[2:]).strip() if len(segs) > 2 else ""
        m_orig = ORIG_RE.search(raw)
        m_now = NOW_RE.search(raw)
        now = round(float(m_now.group(1)), 1) if m_now else None
        orig = round(float(m_orig.group(1)), 1) if m_orig else None
        discount = round(now / orig * 100, 1) if (now and orig) else None
        items.append({
            "标题": title,
            "套餐类型": combo,
            "原价_元": orig,
            "现价_元": now,
            "折扣率_%": discount,
            "套餐内容与限制": extra,
        })
    return items


def band(price: float | None) -> str:
    """R3 价格带分档。"""
    if price is None:
        return "未定价"
    if price < 100:
        return "引流带(<100元)"
    if price <= 200:
        return "主力带(100-200元)"
    return "利润带(>200元)"


def main() -> int:
    ap = argparse.ArgumentParser(description="竞品团购采集：确定性解析与对比统计")
    ap.add_argument("--input", help="输入 JSON（含 source/scope）")
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

    source = str(data.get("source", ""))
    scope = str(data.get("scope", ""))
    if not source.strip():
        print("[错误] source 为空：本技能只解析用户提供的真实摘录，不虚构条目", file=sys.stderr)
        return 2

    items = parse_records(source)
    # R2 超上限按现价从低到高截断
    if len(items) > MAX_ITEMS:
        items = sorted(items, key=lambda x: x["现价_元"] if x["现价_元"] is not None else 9e9)[:MAX_ITEMS]

    priced = [i for i in items if i["现价_元"] is not None]
    bands: dict[str, int] = {}
    for i in priced:
        b = band(i["现价_元"])
        bands[b] = bands.get(b, 0) + 1
    discounts = [i["折扣率_%"] for i in items if i["折扣率_%"] is not None]
    summary = {
        "采集条数": len(items),
        "条数上限": MAX_ITEMS,
        "有现价条数": len(priced),
        "现价最低_元": min(i["现价_元"] for i in priced) if priced else None,
        "现价最高_元": max(i["现价_元"] for i in priced) if priced else None,
        "现价均值_元": round(sum(i["现价_元"] for i in priced) / len(priced), 1) if priced else None,
        "价格带分布": bands,
        "折扣率区间_%": [min(discounts), max(discounts)] if discounts else None,
    }
    for i in items:
        i["价格带"] = band(i["现价_元"])

    result = {"scope": scope, "summary": summary, "items": items}

    os.makedirs(args.outdir, exist_ok=True)
    csv_path = os.path.join(args.outdir, "采集明细.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["标题", "套餐类型", "原价_元", "现价_元",
                                          "折扣率_%", "价格带", "套餐内容与限制"])
        w.writeheader()
        w.writerows(items)
    json_path = os.path.join(args.outdir, "collect.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    lines = [
        "# 竞品团购对比报告",
        "",
        f"- 采集范围：{scope or '（未提供 scope）'}",
        f"- 采集条数：{len(items)} 条（上限 {MAX_ITEMS} 条）",
        f"- 现价区间：{summary['现价最低_元']} - {summary['现价最高_元']} 元，均值 {summary['现价均值_元']} 元",
        f"- 价格带分布：{'；'.join(f'{k} {v} 条' for k, v in bands.items())}",
        "",
        "| # | 标题 | 套餐类型 | 原价(元) | 现价(元) | 折扣率 | 价格带 |",
        "|---|---|---|---|---|---|---|",
    ]
    for n, i in enumerate(items, 1):
        disc = f"{i['折扣率_%']}%" if i["折扣率_%"] is not None else "-"
        orig = i["原价_元"] if i["原价_元"] is not None else "-"
        lines.append(f"| {n} | {i['标题']} | {i['套餐类型']} | {orig} | "
                     f"{i['现价_元']} | {disc} | {i['价格带']} |")
    lines += ["", "> 本报告为 AI 生成内容（确定性脚本产出），发布或决策前需人工复核。", ""]
    md_path = os.path.join(args.outdir, "采集对比报告.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[完成] 采集 {len(items)} 条 → {csv_path} / {json_path} / {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
