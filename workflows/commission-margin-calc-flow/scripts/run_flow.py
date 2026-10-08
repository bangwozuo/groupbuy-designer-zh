# -*- coding: utf-8 -*-
"""
扣点毛利测算工作流 —— 端到端编排脚本（对齐 SKILL.md 步骤链路）。

流程（与 SKILL.md 的 DAG 一致）：
  S1 参数切分（本流脚本：测算参数 / 平台规则材料 / 毛利红线 三段确定性切分）
  S2 毛利测算（脚本承担：调原子技能 groupbuy-pricing-calculate）
  S3 平台规则检索（脚本承担：调原子技能 platform-rules-library，核对费率口径）
  S4 毛利红线校验（本流脚本：经营毛利率 < 红线 → 拦截提示）
  S5 人工确认（不在脚本内）

确定性规则（与 prompt.txt 一致）：
  按「一、二、三、」切分输入三段；金额四舍五入到 0.1 元
  毛利公式与原子技能一致；默认红线为经营毛利率 ≥25%

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
RULES_SCRIPT = os.path.normpath(os.path.join(
    WF_DIR, "..", "..", "skills", "platform-rules-library",
    "scripts", "platform_rules_library.py"))

SEC_RE = re.compile(r"[一二三四]、")
REDLINE_RE = re.compile(r"(?:毛利[率红线]*|经营毛利[率]*)\D*?(\d+(?:\.\d+)?)\s*%")


def split_sections(text: str) -> dict:
    """S1：按「一、二、三、」确定性切分。"""
    parts = SEC_RE.split(text)
    out = {}
    for p in parts[1:]:
        p = p.strip()
        if p.startswith("测算参数") or "到手价" in p[:60]:
            out["params"] = p
        elif "条目" in p[:60] or "平台规则" in p[:30]:
            out["rules_material"] = p
        elif "红线" in p[:30] or REDLINE_RE.search(p[:60]):
            out["redline_text"] = p
    return out


def run_skill(script: str, payload: dict, outdir: str) -> tuple[bool, str]:
    """S2/S3：调原子技能脚本（每步读上一步产物）。"""
    with tempfile.TemporaryDirectory() as td:
        ip = os.path.join(td, "in.json")
        with open(ip, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        r = subprocess.run([sys.executable, script, "--input", ip,
                            "--outdir", outdir], capture_output=True, text=True)
        return r.returncode == 0, (r.stderr or r.stdout).strip()[:200]


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

    # S1 参数切分
    secs = split_sections(raw)
    if "params" not in secs:
        print("[错误] 未找到测算参数段（需含到手价等参数），中止", file=sys.stderr)
        return 2
    # 测算参数缺技术服务费率时，从规则材料段确定性抽取并合并（费率口径以材料为准）
    if "技术服务费" not in secs["params"] and "rules_material" in secs:
        m = re.search(r"技术服务费[^；;。]*?(\d+(?:\.\d+)?)\s*%", secs["rules_material"])
        if m:
            secs["params"] = f"{secs['params']}；平台技术服务费{m.group(1)}%（取自规则材料）"
    os.makedirs(args.outdir, exist_ok=True)
    step1_path = os.path.join(args.outdir, "step1_参数切分.json")
    with open(step1_path, "w", encoding="utf-8") as f:
        json.dump(secs, f, ensure_ascii=False, indent=2)

    # S2 毛利测算
    s2_ok, s2_msg = run_skill(PRICING_SCRIPT,
                              {"params": secs["params"], "rules": ""},
                              os.path.join(args.outdir, "step2_毛利测算"))
    if not s2_ok:
        print(f"[错误] S2 毛利测算失败：{s2_msg}", file=sys.stderr)
        return 2
    with open(os.path.join(args.outdir, "step2_毛利测算", "pricing.json"),
              encoding="utf-8") as f:
        pricing = json.load(f)
    base = pricing["base"]

    # S3 平台规则检索（读规则材料段）
    s3_ok, s3_cites = False, []
    if "rules_material" in secs:
        s3_ok, s3_msg = run_skill(
            RULES_SCRIPT,
            {"query": "平台技术服务费率与达人佣金率口径核对",
             "knowledge": secs["rules_material"]},
            os.path.join(args.outdir, "step3_规则检索"))
        if s3_ok:
            with open(os.path.join(args.outdir, "step3_规则检索", "rules.json"),
                      encoding="utf-8") as f:
                s3_cites = json.load(f)["citations"]

    # S4 毛利红线校验
    redline = 25.0
    if "redline_text" in secs:
        m = REDLINE_RE.search(secs["redline_text"])
        if m:
            redline = float(m.group(1))
    op_rate = base["op_rate"]
    verdict = ("🟢 达标" if op_rate >= redline
               else f"🔴 未达标（{op_rate}% < 红线 {redline}%），拦截：不建议按当前定价上架")

    steps_report = [
        "# 扣点毛利测算工作流报告", "",
        "## 分步结果", "",
        f"1. S1 参数切分：测算参数{'✓' if 'params' in secs else '✗'} / "
        f"规则材料{'✓' if 'rules_material' in secs else '✗'} / 红线 {redline}%",
        f"2. S2 毛利测算：单份毛利 {base['gross']} 元，毛利率 {base['gm_rate']}%，"
        f"经营毛利 {base['op']} 元，经营毛利率 {op_rate}%",
        f"3. S3 平台规则检索：{'成功，召回 ' + str(len(s3_cites)) + ' 条' if s3_ok else '未提供规则材料，跳过（需人工核实费率）'}",
        f"4. S4 毛利红线校验（红线 {redline}%）：{verdict}", "",
        "## 最终交付物", "",
        "| 项目 | 数值 | 说明 |", "|---|---|---|",
        f"| 到手价 | {base['price']} 元 | 团购到手价 |",
        f"| 平台扣点+佣金 | {base['deduct']} + {base['commission']} 元 | 按到手价计 |",
        f"| 单份毛利 / 毛利率 | {base['gross']} 元 / {base['gm_rate']}% | 扣除扣点佣金与食材包装 |",
        f"| 经营毛利 / 经营毛利率 | {base['op']} 元 / {op_rate}% | 再扣固定与人力分摊 |",
        f"| 红线判定（{redline}%） | {verdict} | 校验步 S4 |",
    ]
    if s3_cites:
        steps_report += ["", "## 费率口径引用（来自规则检索）", ""]
        steps_report += [f"- {c}" for c in s3_cites[:4]]
    steps_report += ["", "> 本报告为 AI 生成内容（确定性脚本产出），涉及资金测算仅为草稿，"
                     "需人工复核后再作决策（S5）。", ""]

    result = {
        "summary": f"经营毛利率 {op_rate}%，红线 {redline}% → "
                   f"{'达标' if op_rate >= redline else '未达标拦截'}",
        "steps": {"S1_参数切分": str(sorted(secs.keys())),
                  "S2_毛利测算": f"毛利 {base['gross']} 元 / {base['gm_rate']}%",
                  "S3_平台规则检索": f"{len(s3_cites)} 条引用" if s3_ok else "跳过",
                  "S4_红线校验": verdict,
                  "S5_人工确认": "交付前人工复核"},
        "deliverable": "\n".join(steps_report),
    }
    with open(os.path.join(args.outdir, "flow_result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.outdir, "扣点毛利测算报告.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(steps_report))

    print(f"[完成] 经营毛利率 {op_rate}% vs 红线 {redline}% → {args.outdir}/扣点毛利测算报告.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
