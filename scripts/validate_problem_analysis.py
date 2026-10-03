#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""validate_problem_analysis.py — 校验「题库定位」题解批次（out-prob-*.json / 合并后的文件）。

WHY a separate validator instead of folding these into validate_agent_out.py:
a 题解 is a different artifact from a knowledge card. It has no 概念/公式/teaching layers,
but it MUST have a real solution: 题意澄清、解法步骤、复杂度、**可运行代码**、图解、易错点.
The card validator's shape rules would reject every correct 题解 and accept a 题解-shaped
stub, so the checks have to be written against the artifact that is actually produced.

The checks that matter most, and why each exists:
  · **代码必须是真的** — the reader's complaint was "直接给出仓库链接"; a 题解 whose code
    block is `def solve(): pass` would be a more sophisticated version of the same problem.
    So: minimum length, minimum line count, and a definition/loop/statement must exist.
  · **不得编造 URL** — a 题解 for 牛客题霸 may only link to the source page the corpus already
    has (or a link quoted inside the statement). Anything else is an invented citation.
  · **图解必须自包含** — same SVG rules as the cards (no <image>, no <script>, viewBox,
    字号 ≥ 13, theme colours), because diagrams are rendered offline in six themes.
  · **不确定要标注** — `selfCheck.uncertain` must exist; a 题解 is written from a problem
    statement, and the honest place to record "题面被截断，样例待补" is there.

Usage:
  python scripts/validate_problem_analysis.py --date 2026-10-03
  python scripts/validate_problem_analysis.py --date 2026-10-03 --strict
  python scripts/validate_problem_analysis.py --merged           # web/data/problem-analysis.json
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from validate_agent_out import (  # noqa: E402  (path set above)
    URL_RE, Report, check_diagram, clean_url, is_allowlisted_url, norm_url, read_json,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_DIR = os.path.join(ROOT, "web", "data")

REQUIRED = ("id", "tldr", "restated", "approach", "complexity", "code",
            "diagram", "pitfalls", "selfCheck")
# 题解不要求公式层；但必须有可运行的代码与图解。
CODE_MIN_CHARS = 120
CODE_MIN_LINES = 4
PLACEHOLDER_RE = re.compile(r"^\s*(\.\.\.|pass|# *TODO|# *待补)\s*$", re.I)
CODE_SIGNAL_RE = re.compile(r"\b(def|class|function|for|while|if|return|import)\b|#")


def check_code(where, code, rep):
    if not isinstance(code, dict):
        rep.err(where, "code 必须是对象 {language, source, walkthrough}")
        return
    src = code.get("source")
    if not isinstance(src, str) or not src.strip():
        rep.err(where, "code.source 为空 —— 题解的价值就是可运行的代码")
        return
    if not (code.get("language") or "").strip():
        rep.warn(where, "code.language 缺失（前端按空语言高亮）")
    lines = [ln for ln in src.splitlines() if ln.strip()]
    if len(src) < CODE_MIN_CHARS:
        rep.err(where, "code.source 只有 %d 字符（< %d）：太短，不足以是一份实现"
                 % (len(src), CODE_MIN_CHARS))
    if len(lines) < CODE_MIN_LINES:
        rep.err(where, "code.source 只有 %d 行非空代码（< %d）" % (len(lines), CODE_MIN_LINES))
    if PLACEHOLDER_RE.match(src.strip()):
        rep.err(where, "code.source 是占位符（pass / ... / TODO）")
    if not CODE_SIGNAL_RE.search(src):
        rep.err(where, "code.source 里没有 def/class/for/while/return 等任何代码结构")
    wt = code.get("walkthrough")
    if wt is not None and not isinstance(wt, list):
        rep.err(where, "code.walkthrough 必须是数组")


def check_complexity(where, c, rep):
    if not isinstance(c, dict):
        rep.err(where, "complexity 必须是对象 {time, space, why}")
        return
    for k in ("time", "space"):
        if not (c.get(k) or "").strip():
            rep.err(where, "complexity.%s 缺失" % k)
    if not (c.get("why") or "").strip():
        rep.warn(where, "complexity.why 为空（说清代价来自哪里才算讲明白）")


def check_approach(where, a, rep):
    if not isinstance(a, list) or not a:
        rep.err(where, "approach 必须是非空数组（建议先暴力再最优）")
        return
    if len(a) < 2:
        rep.warn(where, "approach 只有 1 步：建议写清「暴力 → 最优」的取舍")
    for i, step in enumerate(a):
        if isinstance(step, str):
            if not step.strip():
                rep.err(where, "approach[%d] 为空字符串" % i)
            continue
        if not isinstance(step, dict):
            rep.err(where, "approach[%d] 既不是字符串也不是对象" % i)
            continue
        if not (step.get("step") or "").strip() and not (step.get("title") or "").strip():
            rep.err(where, "approach[%d] 缺 step/title" % i)
        if not (step.get("detail") or step.get("text") or "").strip():
            rep.err(where, "approach[%d] 缺 detail/text" % i)


def check_selfcheck(where, sc, rep):
    if not isinstance(sc, dict):
        rep.err(where, "selfCheck 必须是对象")
        return
    for k in ("claims", "uncertain"):
        if k not in sc:
            rep.err(where, "selfCheck.%s 缺失（没有就写空数组）" % k)
        elif not isinstance(sc[k], list):
            rep.err(where, "selfCheck.%s 必须是数组" % k)


def check_provenance(where, entry, allowed, rep):
    """No invented URLs. `allowed` = the problem's own source URL + links in its statement."""
    blob = []
    for key in ("sources",):
        for s in entry.get(key) or []:
            if isinstance(s, dict) and s.get("url"):
                blob.append(s["url"])
    text = "\n".join(str(v) for v in entry.values() if isinstance(v, str))
    blob += [clean_url(u) for u in URL_RE.findall(text)]
    for raw in blob:
        u = clean_url(raw)
        if is_allowlisted_url(u):
            continue
        if norm_url(u) not in allowed:
            rep.err(where, "出现题目来源之外的 URL（禁止编造链接）：%s" % u)


def validate_entries(entries, problems_by_id, rep, label, strict, merged=False):
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            rep.err(label, "entries[%d] 不是对象" % i)
            continue
        pid = str(entry.get("id") or "")
        where = "%s/%s" % (label, pid or ("entries[%d]" % i))
        if not pid:
            rep.err(where, "缺 id")
            continue
        problem = problems_by_id.get(pid)
        if problem is None:
            # In batch mode this means a subagent invented/bent an id (actionable). In
            # --merged mode the file legitimately holds work from earlier runs, so saying
            # "not in this run's queue" would be noise rather than a finding.
            if not merged:
                rep.warn(where, "不在本轮题库队列里（可能是历史批次）")

        for k in REQUIRED:
            if k not in entry:
                rep.err(where, "缺字段 %s" % k)

        tldr = entry.get("tldr") or ""
        if len(tldr) > 80:
            rep.warn(where, "tldr 超过 80 字（%d）" % len(tldr))
        if not tldr.strip():
            rep.err(where, "tldr 为空")
        restated = entry.get("restated") or ""
        if not restated.strip():
            rep.err(where, "restated 为空（题意澄清是题解的第一层）")
        elif len(restated) > 300:
            rep.warn(where, "restated 超过 300 字（%d）" % len(restated))

        check_approach(where, entry.get("approach"), rep)
        check_complexity(where, entry.get("complexity"), rep)
        check_code(where, entry.get("code"), rep)
        check_diagram(where, entry.get("diagram"), rep)
        check_selfcheck(where, entry.get("selfCheck"), rep)

        for k in ("pitfalls", "edgeCases"):
            v = entry.get(k)
            if v is not None and not isinstance(v, list):
                rep.err(where, "%s 必须是数组" % k)
        if not (entry.get("pitfalls") or []):
            rep.err(where, "pitfalls 为空：手撕题的扣分点必须写出来")
        st = entry.get("selfTest")
        if st is not None and (not isinstance(st, list) or len(st) < 2):
            rep.warn(where, "selfTest 少于 2 题")

        allowed = set()
        if problem:
            if problem.get("sourceUrl"):
                allowed.add(norm_url(problem["sourceUrl"]))
            for u in URL_RE.findall(str(problem.get("statement") or "")):
                allowed.add(norm_url(clean_url(u)))
        check_provenance(where, entry, allowed, rep)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=None, help="YYYY-MM-DD（默认今天）")
    ap.add_argument("--work-dir", default=None,
                    help="默认 scripts/agent-work-<date>")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--merged", action="store_true",
                    help="改为校验 web/data/problem-analysis.json 的 byId")
    ap.add_argument("--strict", action="store_true", help="警告也算失败")
    ap.add_argument("--only", default=None,
                    help="只校验这些 id（逗号分隔）。并行写批次时用，避免被别的批次"
                         "正在写一半的文件干扰。")
    args = ap.parse_args(argv)

    import datetime as _dt
    date = args.date or _dt.date.today().isoformat()
    data_dir = args.data_dir if os.path.isabs(args.data_dir) else os.path.join(ROOT, args.data_dir)
    work_dir = args.work_dir or os.path.join(ROOT, "scripts", "agent-work-%s" % date)

    plan = read_json(os.path.join(data_dir, "deep-read-plan.json"), {}) or {}
    plan_problems = {str(p.get("id")): p for p in (plan.get("problems") or [])}

    # The URL allow-list must come from the BANK, not from this run's queue.
    #
    # MEASURED BUG (this validator's own first version): `--merged` built the id->problem map
    # from `plan.problems`, which by construction contains only the problems that are STILL
    # un-analysed. So every already-written 题解 was checked against an empty allow-list and
    # its legitimate source URL was reported as "编造链接" — 12 false errors per run, which
    # would have trained the reader to ignore the check entirely.
    bank = read_json(os.path.join(data_dir, "problem-bank.json"), {}) or {}
    problems_by_id: dict[str, dict] = {}
    for p in bank.get("problems") or []:
        if isinstance(p, dict) and p.get("id"):
            problems_by_id[str(p["id"])] = p
    # 人工整理的题目（jobs_kb）也要能校验：它们的题解是合法产物，只是不在本轮
    # `problems` 队列里（队列只放"待解析"的采集题）。没有这一段，为人工手撕题写的题解
    # 会被报成"不在题库队列里"，而它恰恰是读者最想要的（手写 MHA / RoPE / LoRA）。
    #
    # 注意要同时读 `curatedAnalyzed`：build_problem_bank.py 把"已有题解"的人工题从
    # `curated`（待办清单）移到这里，只读 `curated` 就会把已经写完的题解误报成历史批次。
    for key in ("curated", "curatedAnalyzed"):
        for c in bank.get(key) or []:
            if isinstance(c, dict) and c.get("id"):
                problems_by_id.setdefault(str(c["id"]), {
                    "id": str(c["id"]),
                    "kind": c.get("kind"),
                    "title": c.get("title"),
                    "statement": c.get("statement"),
                    "sourceUrl": None,
                    "origin": "curated",
                })
    # The plan's copy wins: it carries the statement as it was when the work was handed out.
    problems_by_id.update(plan_problems)

    rep = Report()
    label = "merged" if args.merged else os.path.basename(work_dir)
    if args.merged:
        doc = read_json(os.path.join(data_dir, "problem-analysis.json"), {}) or {}
        entries = list((doc.get("byId") or {}).values())
        print("merged problem-analysis.json: %d entries" % len(entries))
    else:
        files = sorted(glob.glob(os.path.join(work_dir, "out-prob-*.json")))
        entries = []
        for path in files:
            doc = read_json(path, {}) or {}
            got = doc.get("entries")
            if not isinstance(got, list):
                rep.err(os.path.basename(path), "entries 不是数组")
                continue
            entries.extend(got)
        print("题解批次文件: %d 个，条目 %d 条" % (len(files), len(entries)))
        if not files and problems_by_id:
            print("  (本轮题库队列 %d 题，但没有 out-prob-*.json)" % len(problems_by_id))

    if args.only:
        keep = {s.strip() for s in args.only.split(",") if s.strip()}
        before = len(entries)
        entries = [e for e in entries if isinstance(e, dict) and str(e.get("id")) in keep]
        print("--only %s：%d -> %d 条" % (sorted(keep), before, len(entries)))
        if not entries:
            rep.err("--only", "没有匹配到任何题解条目")

    validate_entries(entries, problems_by_id, rep, label, args.strict, merged=args.merged)

    for w in rep.warnings:
        print("WARN  %s" % w)
    for e in rep.errors:
        print("ERROR %s" % e)
    print("")
    print("结论: %d 条题解，errors=%d warnings=%d" % (len(entries), len(rep.errors), len(rep.warnings)))
    if rep.errors:
        print("题解校验失败：修好再合入 web/data（否则题库定位会显示半成品）")
        return 1
    if args.strict and rep.warnings:
        print("--strict：存在警告，视为失败")
        return 1
    print("题解校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
