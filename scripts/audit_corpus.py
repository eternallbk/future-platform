#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_corpus.py — 对现有全部知识卡片做一次"是否值得积累"的体检，并输出可执行工单

为什么需要它（而不是只看 selfcheck）
  selfcheck 回答"数据是否可信/构成是否合理"；它不会告诉你**具体哪一条**应该下架。
  用户的诉求是"自检一遍现有知识卡片是否冗余、是否与求职无关"，那就必须落到条目级：
  哪些是噪音、哪些是可下架的重复/无关内容、哪些应该被深读优先照顾。

判定原则（与采集端同一套 knowledgeType + 证据口径，避免两处标准不一）
  淘汰：资讯/观点且无关键词支撑、与 AI 无关的论文、明确无关的工具类
  保留：深度解析、面经、算法题、八股、岗位、框架/实现
  观察：未归类（other）—— 不是错误，但要看是不是关键词覆盖不足

用法:
  python scripts/audit_corpus.py                 # 报告
  python scripts/audit_corpus.py --json out.json # 机器可读
  python scripts/audit_corpus.py --list          # 逐条列出建议下架项
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "web" / "data"
CST = timezone(timedelta(hours=8))

# Material that has nothing to do with AI/algorithm work. Calibrated by reading the
# actual corpus, and deliberately NOT a list of domain words: an earlier version matched
# bare `linguistic`, `grammar` and `alzheimer`, which flagged two legitimate VLM papers
# ("Devils in Question Relay", relevance 87.1, and "It's Not What the Image Shows", 67.7)
# for deletion. A removal tool that deletes good papers is worse than no tool, so the
# patterns below are specific phrases rather than general vocabulary.
OFF_TOPIC_RE = re.compile(
    r"MARITAL HEALTH|PREDICTORS AND CORRELATES OF MARITAL|BEYOND SATISFACTION|"
    r"POLYMER INFORMATICS|polymer informatics|"
    r"PORTFOLIO ASSESSMENT|chemotherapy response|"
    r"Alzheimer.?(?:'s)? (?:disease|detection|diagnosis|classification)|"
    r"dental (?:caries|implant|radiograph)|nursing (?:home|care|student)|"
    r"asthma|diabetes (?:mellitus|retinopathy)|"
    r"grammar and pragmatics|Dialogic Resonance|"
    r"supermarket|retail store|hotel booking|tourism|"
    r"比亚迪闪充|国轩高科|充电站|电池产业|"
    r"password model|password guessing|Password Modeling",
    re.I)
GENERIC_TOOL_RE = re.compile(
    r"\b(?:wallpaper|screenshot tool|password manager|bookmark manager|"
    r"file manager|download manager|dotfiles|"
    r"track(?:s|ing)? (?:location|phone|mobile)|spyware|adblock|"
    r"emoji picker|color picker)\b", re.I)

# A curated list ABOUT a technical subject is learning material, not a tool list.
# Must stay in sync with collect.is_generic_tool() - the audit and the collector have to
# agree, or a card the collector keeps gets recommended for deletion.
CURATED_LIST_RE = re.compile(
    r"awesome[- ]|curated list|a list of (?:papers|resources|models|datasets)|"
    r"paper list|资源合集|汇总列表", re.I)
CURATED_TECH_SUBJECT_RE = re.compile(
    r"diffusion|vla|vln|vlm|lvlm|llm|language model|video|vision|multimodal|"
    r"post-?training|reinforcement|rlhf|agent|transformer|attention|"
    r"embodied|robot|world model|segment|detection|generation|"
    r"算法|论文|模型|多模态|大模型|强化学习|具身|机器人", re.I)

# Commercial spam that keyword-matched into the corpus.
SPAM_AD_RE = re.compile(
    r"品茶|茶工作室|海选|技师|上门服务|会所|桑拿|"
    r"贷款|办卡|刷单|兼职日结|引流|",
    re.I)


def _collector_spam_reason(item: dict) -> str:
    """Ask the COLLECTOR whether it considers this item spam.

    WHY this delegates instead of keeping its own word list: the audit originally had a
    parallel SPAM_AD_RE containing commerce words like 订阅, and it drifted - it flagged
    the legitimate 八股 card "4.2.2 银行技术面---技术基础问题（操作系统）" (whose body
    merely contains 订阅) while the collector correctly kept it. A single source of truth
    is the only way two judgements about the same item stay consistent; `audit_corpus.py`
    must never recommend deleting something the collector happily accepts.
    """
    try:
        import importlib.util
        from pathlib import Path as _P
        spec = importlib.util.spec_from_file_location(
            "collect_for_audit", _P(__file__).with_name("collect.py"))
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        return mod.spam_reason(item)
    except Exception:
        # Never let a delegation failure turn into a false deletion: fall back to the
        # narrow spam-only pattern above, which cannot match technical prose.
        blob = f"{item.get('title') or ''} {str(item.get('summary') or '')[:400]}"
        return "推广/垃圾内容（非知识积累）" if SPAM_AD_RE.search(blob) else ""


def is_generic_tool(hay: str) -> bool:
    if not GENERIC_TOOL_RE.search(hay):
        return False
    if CURATED_LIST_RE.search(hay) and CURATED_TECH_SUBJECT_RE.search(hay):
        return False
    return True

SUBSTANTIVE = ("method", "interview", "coding", "fundamentals", "project", "job")


def load(p: Path, fb):
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return fb


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    ap.add_argument("--list", action="store_true", help="逐条列出建议下架项")
    args = ap.parse_args()

    items = (load(DATA / "items" / "index.json", {}) or {}).get("items") or []
    enrich = (load(DATA / "enrichment.json", {}) or {}).get("byId") or {}
    if not items:
        print("no items")
        return 1

    n = len(items)
    kt = Counter(str(i.get("knowledgeType") or "other") for i in items)
    zh = {"method": "算法深度解析/论文", "interview": "面经/面试", "coding": "算法题/手撕",
          "fundamentals": "八股/基础", "job": "岗位/招聘", "project": "框架/项目",
          "news": "资讯", "opinion": "观点/吐槽", "other": "未归类"}

    print("")
    print("=" * 78)
    print(f"知识卡片体检   共 {n} 条   已深读 {len(enrich)} 条")
    print("=" * 78)
    print()
    print("【构成】")
    for k, v in kt.most_common():
        print(f"  {zh.get(k, k):<16} {v:>4}  {v / n:6.1%}")

    # ---- per-item verdicts -------------------------------------------------
    drop: list[dict] = []
    watch: list[dict] = []
    for it in items:
        iid = it.get("id")
        title = str(it.get("title") or "")
        summ = str(it.get("summary") or "")
        hay = f"{title} {summ[:400]}"
        k = str(it.get("knowledgeType") or "other")
        hits = it.get("categoryHits") or []
        rel = float(it.get("relevanceScore") or 0)

        reason = None
        if _collector_spam_reason(it):
            reason = "推广/垃圾内容（非知识积累）"
        elif OFF_TOPIC_RE.search(hay) and k not in ("interview", "coding"):
            reason = "与 AI/算法无关的内容"
        elif is_generic_tool(hay):
            reason = "通用工具类，无技术积累价值"
        elif k in ("news", "opinion") and not hits:
            reason = f"{zh[k]}且无方向关键词"
        elif k == "other" and not hits:
            reason = "未归类且未命中任何方向关键词"
        elif rel < 35:
            reason = f"相关度过低（{rel:.0f} < 35）"

        if reason:
            drop.append({"id": iid, "title": title[:70], "category": it.get("category"),
                         "knowledgeType": k, "relevance": rel, "reason": reason,
                         "deepRead": iid in enrich})
        elif k == "other":
            watch.append({"id": iid, "title": title[:70], "category": it.get("category"),
                          "relevance": rel})

    print()
    print("【建议下架】")
    if not drop:
        print("  无")
    else:
        by_reason = Counter(d["reason"] for d in drop)
        for r, c in by_reason.most_common():
            print(f"  {r}: {c} 条")
        print(f"  合计 {len(drop)} 条（占 {len(drop) / n:.1%}）；"
              f"其中已被深读 {sum(1 for d in drop if d['deepRead'])} 条")
        if args.list:
            print()
            for d in sorted(drop, key=lambda x: x["relevance"]):
                print(f"    [{d['relevance']:>5.1f}][{d['knowledgeType']:<11}] {d['title']}")

    print()
    print("【待观察：未归类】")
    print(f"  {len(watch)} 条 —— 不是错误，但若持续增长说明关键词覆盖不足")
    if args.list:
        for w in sorted(watch, key=lambda x: x["relevance"])[:20]:
            print(f"    [{w['relevance']:>5.1f}] {w['title']}")

    print()
    print("【深读优先级建议】")
    # The deep-read quota should spend itself on accumulating knowledge, so report how
    # many deep-readable items exist per knowledge type.
    for k in ("coding", "fundamentals", "interview", "method", "job"):
        tot = kt.get(k, 0)
        done = sum(1 for i in items if (i.get("knowledgeType") == k) and i.get("id") in enrich)
        if tot:
            print(f"  {zh[k]:<16} 已深读 {done:>3}/{tot:<4} ({done / tot:5.1%})")

    print()
    print("=" * 78)
    print("下一步：python scripts/prune_items.py --apply   （下架会写入 blocklist，不会复活）")
    print("=" * 78)
    print("")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"generatedAt": datetime.now(CST).isoformat(timespec="seconds"),
             "corpus": n, "knowledgeTypes": dict(kt), "drop": drop, "watch": watch},
            ensure_ascii=False, indent=1), "utf-8")
        print(f"报告已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
