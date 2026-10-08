# -*- coding: utf-8 -*-
"""
团购标题与图片文案工作流 —— 端到端编排脚本（对齐 SKILL.md 步骤链路）。

流程（与 SKILL.md 的 DAG 一致）：
  S1 团购文案生成（模型步：按原子技能 groupbuy-copy-generate 的 prompt.txt 产出
     3 个标题（每个 ≤20 字）+ 五段式正文（150-300 字））
  S2 排版确定性校验（本流脚本承担）：标题字数、正文字数区间、必写要点数字覆盖、
     图片排版清单生成
  S3 人工确认（不在脚本内）

确定性校验规则（与 prompt.txt 一致）：
  R1 标题 3 个备选，每个 ≤20 字
  R2 正文总长 150-300 字（去除标点后按字符实数计）
  R3 必写要点中的关键数字（价格/数量/天数/时段）必须在正文出现，缺一项即 fail
  R4 图片清单：主图 1 张（首图信息点 ≤5 个）+ 细节图 ≥3 张 + 规则说明图 1 张

用法：
  python run_flow.py --input input.json --outdir out      # input 含 S1 模型产出 draft
  python run_flow.py --demo                               # 内置真实文案样例代跑 S1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WF_DIR = os.path.dirname(HERE)

PUNCT_RE = re.compile(r"[，。！？；：、（）\"\"''\s\n,.:;()!?…—·]")

# 内置 demo：S1 模型步的真实产出样例（锦城巷串串香双人套餐，与 runs_v2 实跑一致）
DEMO_DRAFT = {
    "titles": [
        {"text": "春熙路8年串串香老店，双人88元晚市畅签", "主打卖点": "资质+价格锚点"},
        {"text": "工作日晚饭不将就：现炒锅底+80支签签", "主打卖点": "场景"},
        {"text": "加班后的犒劳：免预约串串双人餐", "主打卖点": "人群/便利"},
    ],
    "body": ("加班到七点，最想要一锅热气腾腾的红汤。锦城巷串串香·春熙路店，本地开了8年的社区老店，"
             "牛油锅底每日现炒，签签每日冷链直达，不是料理包，是你在成都街头该吃到的味道。"
             "【双人套餐 88元 | 原价136元】锅底任选1份（牛油/番茄/菌汤）；签签荤素各40支；"
             "冰粉2份；酸梅汤1扎。周一到周五17:00后到店，免预约、直接核销，下班路上顺路就能吃。"
             "套餐有效期90天，过期未用自动退款，不怕囤着浪费。点击抢购，今晚就去涮。"),
    "key_points_text": ("双人套餐88元（原价136元）；签签荤素各40支；冰粉2份；酸梅汤1扎；"
                        "周一至周五17:00后免预约核销；有效期90天，过期自动退款"),
}

TITLE_LIMIT = 20
BODY_RANGE = (150, 300)
KEYNUM_RE = re.compile(r"\d+(?:\.\d+)?(?:元|支|份|扎|天|:00|点)")


def check_titles(titles: list[dict]) -> list[dict]:
    rows = []
    for i, t in enumerate(titles, 1):
        n = len(t["text"])
        rows.append({"检查项": f"标题{i}", "标准": f"≤{TITLE_LIMIT} 字", "实测": f"{n} 字",
                     "判定": "✅ 通过" if n <= TITLE_LIMIT else f"🔴 超 {n - TITLE_LIMIT} 字"})
    if len(titles) != 3:
        rows.append({"检查项": "标题数量", "标准": "3 个备选", "实测": f"{len(titles)} 个",
                     "判定": "🔴 不足，退回 S1"})
    else:
        rows.append({"检查项": "标题数量", "标准": "3 个备选", "实测": "3 个", "判定": "✅ 通过"})
    return rows


def check_body(body: str) -> list[dict]:
    n = len(PUNCT_RE.sub("", body))
    ok = BODY_RANGE[0] <= n <= BODY_RANGE[1]
    return [{"检查项": "正文字数（去标点）", "标准": f"{BODY_RANGE[0]}-{BODY_RANGE[1]} 字",
             "实测": f"{n} 字",
             "判定": "✅ 通过" if ok else (f"🔴 偏少，补 {BODY_RANGE[0] - n} 字" if n < BODY_RANGE[0]
                                           else f"🔴 超长，删 {n - BODY_RANGE[1]} 字")}]


def check_keynums(body: str, key_text: str) -> list[dict]:
    nums = sorted(set(KEYNUM_RE.findall(key_text)))
    rows = []
    for num in nums:
        hit = num in body
        rows.append({"检查项": f"要点覆盖 {num}", "标准": "正文必须出现",
                     "实测": "出现" if hit else "缺失", "判定": "✅ 通过" if hit else "🔴 缺失"})
    return rows


def image_plan(titles: list[dict], body: str) -> list[dict]:
    """R4 图片排版清单（确定性生成）。"""
    return [
        {"图位": "主图", "内容": f"推荐标题「{titles[0]['text'][:TITLE_LIMIT]}」+ 套餐主视觉",
         "规则": "首图信息点 ≤5 个（价格/原价/数量/时段/资质）"},
        {"图位": "细节图 1", "内容": "套餐清单逐项图（价格与数量与正文一致）", "规则": "每图 1 个信息组"},
        {"图位": "细节图 2", "内容": "门店资质/现制过程", "规则": "只用用户提供的真实资质"},
        {"图位": "细节图 3", "内容": "使用时段与免预约提示", "规则": "时段数字与正文一致"},
        {"图位": "规则图", "内容": "有效期与退款规则", "规则": "天数/退款口径与正文一致"},
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="输入 JSON（含 input 与 S1 模型产出 draft）")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--outdir", default="out")
    args = ap.parse_args()

    if args.demo:
        draft = DEMO_DRAFT
        brief = "锦城巷串串香春熙路店抖音团购文案（demo 代跑 S1 模型步）"
    elif args.input:
        with open(args.input, encoding="utf-8") as f:
            data = json.load(f)
        draft = data.get("draft") or {}
        brief = str(data.get("input", ""))
        if not draft.get("titles") or not draft.get("body"):
            print("[错误] 缺少 S1 模型产出 draft（titles/body）。请先按原子技能 "
                  "groupbuy-copy-generate 的 prompt.txt 产出文案，再以 draft 字段传入。",
                  file=sys.stderr)
            return 2
    else:
        print("[错误] 需要 --input 或 --demo", file=sys.stderr)
        return 2

    titles = draft["titles"]
    body = draft["body"]
    key_text = draft.get("key_points_text", "")

    # S2 确定性校验
    rows = check_titles(titles) + check_body(body) + (check_keynums(body, key_text) if key_text else [])
    n_fail = sum(1 for r in rows if "🔴" in r["判定"])
    verdict = "✅ 通过" if n_fail == 0 else f"🔴 {n_fail} 项不通过，退回 S1 修改"

    os.makedirs(args.outdir, exist_ok=True)
    step1_path = os.path.join(args.outdir, "step1_文案草稿.json")
    with open(step1_path, "w", encoding="utf-8") as f:
        json.dump(draft, f, ensure_ascii=False, indent=2)

    # 读上一步产物再汇总（保持步骤链路）
    with open(step1_path, encoding="utf-8") as f:
        s1 = json.load(f)

    plan = image_plan(s1["titles"], s1["body"])
    report = [
        "# 团购标题与图片文案校验报告", "",
        f"- S1 文案生成（模型步）：{brief}",
        f"- S2 排版校验：共 {len(rows)} 项，通过 {len(rows) - n_fail} 项，不通过 {n_fail} 项 → {verdict}", "",
        "| 检查项 | 标准 | 实测 | 判定 |", "|---|---|---|---|",
    ] + [f"| {r['检查项']} | {r['标准']} | {r['实测']} | {r['判定']} |" for r in rows]
    report += ["", "## 图片排版清单", "",
               "| 图位 | 内容 | 规则 |", "|---|---|---|",
               ] + [f"| {p['图位']} | {p['内容']} | {p['规则']} |" for p in plan]
    report += ["", "> 本报告为 AI 生成内容（S2 为确定性脚本产出），发布前需人工确认（S3）。", ""]

    result = {
        "summary": f"排版校验 {len(rows)} 项，不通过 {n_fail} 项 → {verdict}",
        "steps": {"S1_文案生成": "模型步（groupbuy-copy-generate prompt）",
                  "S2_排版校验": f"{len(rows)} 项检查 + 图片排版清单",
                  "S3_人工确认": "发布前人工确认"},
        "deliverable": "\n".join(report),
    }
    with open(os.path.join(args.outdir, "flow_result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.outdir, "文案排版校验报告.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(report))

    print(f"[完成] 校验 {len(rows)} 项（不通过 {n_fail}）→ {args.outdir}/文案排版校验报告.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
