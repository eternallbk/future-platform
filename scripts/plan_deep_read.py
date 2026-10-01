#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
plan_deep_read.py — 每日「深度解析配额」规划器

背景（你要的两件事）
  1. 不是所有采集到的条目都值得做深度解析。招聘帖、灌水讨论、重复转载这类内容
     进了卡片库只会造成冗余，还会稀释「今日速览」的信号。
  2. 你要的是**每个分类 2–10 条、有深度解析**，而不是几百条只有摘要的卡片。

所以这一步做两件事：
  A. **质量闸门**：给每条打分一个「是否值得深度解析」的判定，低价值内容标为
     `skim`（仍保留在库里可搜索、参与去重，但不会出现在深度解析队列里，
     也不会占用「今日速览」的位置）。
  B. **配额分配**：按分类给出 2–10 条的名额，用「相关度 + 内容完整度 + 类型多样性」
     排序挑出该深读的条目，写成一个**给 Agent 用的明确任务清单**。

为什么单独一个脚本：
  确定性层（collect.py）不该知道「深度解析」这件事，而 Agent 层需要一个稳定的
  输入契约。这个脚本把两者接起来，并且可以在不跑 Agent 的情况下单独检查配额是否合理。

用法:
  python scripts/plan_deep_read.py                 # 生成今日计划
  python scripts/plan_deep_read.py --per-category-min 3 --per-category-max 8
  python scripts/plan_deep_read.py --show          # 打印计划详情
退出码: 0 = 正常（即使队列为空）
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "web" / "data"
CST = timezone(timedelta(hours=8))
PLAN = DATA / "deep-read-plan.json"

# ---------------------------------------------------------------------------
# 质量闸门规则
# ---------------------------------------------------------------------------
# 这些是**模式**而不是黑名单网站：同一个站点既有高价值内容也有灌水。
# 每条规则都要给出理由，方便日后复核是否误杀。
NOISE_RULES = [
    {
        "id": "career-vent",
        "why": "求职吐槽/情绪帖：信息量为零，且会大量重复",
        "patterns": [r"处境.*未来", r"勤勤恳恳", r"不摸鱼", r"升职加薪.*轮不到",
                     r"要不要跑", r"劝退", r"后悔", r"焦虑", r"破防", r"内卷.*值得"],
    },
    {
        "id": "generic-question",
        "why": "泛化提问帖：没有可结构化的知识",
        "patterns": [r"^为什么很多人", r"^如何评价", r"^大家怎么看", r"^有人知道吗",
                     r"^请问.*怎么样$", r"^求推荐", r"^求问"],
    },
    {
        "id": "referral-code",
        "why": "内推码/广告：有用但不需要深度解析，单独归入岗位信息即可",
        "patterns": [r"内推码", r"内推链接", r"扫.*码.*投递", r"急招.*加微信"],
    },
    {
        "id": "salary-thread",
        "why": "纯薪资讨论：数字无法核实，且极易重复",
        "patterns": [r"薪资.*爆料", r"offer.*对比.*薪资", r"总包.*多少"],
    },
]

# 分类的价值权重：决定配额怎么倾斜。核心方向多给名额，趋势类少给。
CATEGORY_DEPTH_VALUE = {
    "multimodal": 1.00,
    "posttraining": 1.00,
    "worldmodel": 1.00,
    "rl": 0.90,
    "generative": 0.85,
    "coding": 1.00,
    "paper": 0.95,
    "foundation": 0.70,
    "engineering": 0.65,
    "agent": 0.60,
    "course": 0.55,
    "job": 0.50,      # 岗位信息进岗位看板，不需要逐条深度解析
    "exam": 0.80,
    "trend": 0.45,    # 趋势类留少量即可
}

# 值得深度解析的来源（同行评审 / 官方博客 / 一手仓库）
HIGH_TRUST_CHANNELS = {"arxiv", "openreview", "hf_papers", "s2", "openai_rss",
                       "hf_blog_rss", "github", "gh_trending", "inbox"}


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def load(path: Path, fallback):
    try:
        if path.exists():
            return json.loads(path.read_text("utf-8"))
    except Exception as e:
        print(f"[warn] {path.name}: {e}", file=sys.stderr)
    return fallback


def category_mismatch(item: dict) -> str | None:
    """Return a reason when the item cannot plausibly belong to its category.

    Why this exists: the keyword classifier assigns a category from word hits, so
    unrelated items land in specialised buckets and then consume that bucket's
    deep-read quota. Measured on 2026-10-01, the top-scoring plan entry for
    `course` was "Algorithmic Justice and Responsible AI Journalism" and the top
    `exam` entry was "FIREWALL: A surrogate model for ..." - neither is a course
    or a practice problem. Surfacing them under those headings is worse than
    leaving the quota unfilled, so they are excluded from the plan.

    These categories expect an explicit marker; without it the item is treated as
    misclassified rather than silently relabelled.
    """
    cat = item.get("category")
    blob = f"{item.get('title') or ''} {item.get('summary') or ''}".lower()
    if cat == "course":
        if not re.search(r"课程|课|lecture|tutorial|mooc|coursera|cs\d{2,3}|"
                         r"公开课|读书会|walkthrough|bootcamp|教材|textbook|系列教程", blob):
            return "看起来不是课程（缺少课程/lecture/tutorial/公开课等标志）"
    elif cat == "exam":
        if not re.search(r"面试|笔试|手撕|手写|面经|真题|leetcode|算法题|编程题|"
                         r"八股|onsite|interview|coding (?:problem|question)|"
                         r"take-?home|题目|题库", blob):
            return "看起来不是笔试题（缺少面试/手撕/LeetCode/真题等标志）"
    elif cat == "job":
        if not re.search(r"工程师|研究员|实习|招聘|岗位|offer|算法专家|engineer|intern|"
                         r"scientist|developer|hiring|job|校招|社招|薪资|package", blob):
            return "看起来不是岗位信息（缺少招聘/工程师/实习等标志）"
    elif cat == "coding":
        if not re.search(r"算法题|手撕|手写|leetcode|题解|面经|八股|动态规划|"
                         r"binary search|two pointers|滑动窗口|并查集|单调栈|"
                         r"interview|coding|problem|solution", blob):
            return "看起来不是手撕题（本文归类可能错误）"
    elif cat == "unclassified":
        # No keyword matched at all. There is nothing to deep-read against a
        # category, and spending a quota slot on it would take that slot away from a
        # real direction. These items are reported instead (see the proposal's
        # noiseReport) so the keyword set can be extended deliberately.
        return "未命中任何分类关键词（应扩充关键词，而不是给它深度解析）"
    return None


def noise_reason(item: dict) -> str | None:
    """Return the rule id that marks this item as not worth a deep read."""
    title = str(item.get("title") or "")
    summary = str(item.get("summary") or "")
    blob = f"{title} {summary}"
    for rule in NOISE_RULES:
        for p in rule["patterns"]:
            if re.search(p, blob):
                # A referral code inside a real job posting is fine; only flag it
                # when the item is essentially just the code.
                if rule["id"] == "referral-code" and len(summary) > 200:
                    continue
                return rule["id"]
    return None


def depth_score(item: dict) -> tuple[float, dict]:
    """How much does this item deserve a deep read? Higher is better."""
    rel = float(item.get("relevanceScore") or 0)
    cat = item.get("category") or "trend"

    content = min(1.0, len(str(item.get("summary") or "")) / 900.0)
    has_code = 1.0 if item.get("codeAvailable") else 0.0
    peer = 1.0 if item.get("peerReviewed") else 0.0
    trust = 1.0 if item.get("channel") in HIGH_TRUST_CHANNELS else 0.0
    is_material = 0.0
    # A paper/repo/course card can carry depth; a pure news blurb usually cannot.
    if cat in ("paper", "multimodal", "posttraining", "worldmodel", "generative",
               "rl", "coding", "foundation", "engineering", "agent"):
        is_material = 1.0

    parts = {
        "relevance": rel * 0.45,
        "content": content * 100 * 0.18,
        "code": has_code * 100 * 0.08,
        "peer": peer * 100 * 0.07,
        "trust": trust * 100 * 0.07,
        "material": is_material * 100 * 0.15,
    }
    catw = CATEGORY_DEPTH_VALUE.get(cat, 0.5)
    return round(sum(parts.values()) * (0.6 + 0.4 * catw), 2), parts


def build_plan(per_min: int, per_max: int, fresh_only: bool) -> dict:
    index = load(DATA / "items" / "index.json", {}) or {}
    items = index.get("items") or []
    digest = load(DATA / "digest" / "today.json", {}) or {}
    fresh_ids = {i.get("id") for i in (digest.get("items") or [])}
    enrichment = (load(DATA / "enrichment.json", {}) or {}).get("byId") or {}

    # Candidates: not already enriched, and (optionally) only today's fresh items.
    pool = [i for i in items
            if i.get("id") not in enrichment
            and (not fresh_only or i.get("id") in fresh_ids)]

    by_cat: dict[str, list] = {}
    skim: list[dict] = []
    for it in pool:
        reason = noise_reason(it)
        if reason:
            skim.append({"id": it.get("id"), "title": (it.get("title") or "")[:90],
                         "category": it.get("category"), "rule": reason,
                         "why": next(r["why"] for r in NOISE_RULES if r["id"] == reason)})
            it["_noise"] = reason
            continue
        mismatch = category_mismatch(it)
        if mismatch:
            skim.append({"id": it.get("id"), "title": (it.get("title") or "")[:90],
                         "category": it.get("category"), "rule": "category-mismatch",
                         "why": mismatch})
            continue
        by_cat.setdefault(it.get("category") or "trend", []).append(it)

    selected: list[dict] = []
    per_category: dict[str, dict] = {}
    # Learning signals let the quota follow what the reader actually studies.
    # Without this the plan is blind to the reader's own behaviour and always
    # spends depth on the same static category weights.
    feedback = load(DATA / "feedback.json", {}) or {}
    fb_cat = feedback.get("byCategory") or {}
    max_fb = max(fb_cat.values()) if fb_cat else 0

    for cat, rows in sorted(by_cat.items()):
        scored = []
        for it in rows:
            s, parts = depth_score(it)
            # A small depth bonus for categories the reader engages with, capped at
            # +6 so an interest cannot starve the other directions.
            if max_fb:
                s = round(s + 6.0 * (fb_cat.get(cat, 0) / max_fb), 2)
            scored.append((s, parts, it))
        scored.sort(key=lambda x: (-x[0], str(x[2].get("id"))))

        catw = CATEGORY_DEPTH_VALUE.get(cat, 0.5)
        # Interest can lift the quota by up to +1 slot (never below the base).
        if max_fb and fb_cat.get(cat):
            catw = min(1.0, catw + 0.1 * (fb_cat[cat] / max_fb))
        quota = int(round(per_min + (per_max - per_min) * catw))
        quota = max(per_min, min(per_max, quota))

        picked = []
        # Diversify by channel so one chatty source cannot fill a category.
        seen_channel: dict[str, int] = {}
        for s, parts, it in scored:
            if len(picked) >= quota:
                break
            ch = str(it.get("channel") or "unknown")
            if seen_channel.get(ch, 0) >= max(2, quota // 2):
                continue
            seen_channel[ch] = seen_channel.get(ch, 0) + 1
            picked.append({
                "id": it.get("id"), "title": it.get("title"),
                "category": cat, "channel": ch,
                "relevanceScore": it.get("relevanceScore"),
                "depthScore": s, "url": it.get("url"),
                "hasSummary": bool(str(it.get("summary") or "").strip()),
                "scoreBreakdown": parts,
            })
        # If diversity filtering starved the category, top up from the remainder.
        if len(picked) < quota:
            taken = {p["id"] for p in picked}
            for s, parts, it in scored:
                if len(picked) >= quota:
                    break
                if it.get("id") in taken:
                    continue
                picked.append({
                    "id": it.get("id"), "title": it.get("title"),
                    "category": cat, "channel": str(it.get("channel") or "unknown"),
                    "relevanceScore": it.get("relevanceScore"),
                    "depthScore": s, "url": it.get("url"),
                    "hasSummary": bool(str(it.get("summary") or "").strip()),
                    "scoreBreakdown": parts,
                })
        selected += picked
        per_category[cat] = {
            "quota": quota, "picked": len(picked),
            "candidates": len(rows), "categoryWeight": catw,
        }

    selected.sort(key=lambda x: -x["depthScore"])
    return {
        "generatedAt": now_iso(),
        "generatedBy": "plan_deep_read.py",
        "policy": {
            "perCategoryMin": per_min,
            "perCategoryMax": per_max,
            "freshOnly": fresh_only,
            "note": ("每个分类挑 2–10 条做深度解析；其余条目仍保留在卡片库里可搜索、"
                     "参与去重，但不会进入深度解析队列，也不会挤占「今日速览」。"),
            "noiseRules": [{"id": r["id"], "why": r["why"]} for r in NOISE_RULES],
        },
        "totals": {
            "corpus": len(items),
            "alreadyEnriched": len(enrichment),
            "candidates": len(pool),
            "skimmed": len(skim),
            "selected": len(selected),
            "categories": len(per_category),
        },
        "perCategory": per_category,
        "queue": selected,
        "skimSample": skim[:40],
        "skimByRule": _count_by(skim, "rule"),
    }


def _count_by(rows: list[dict], key: str) -> dict:
    out: dict = {}
    for r in rows:
        out[str(r.get(key))] = out.get(str(r.get(key)), 0) + 1
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="plan_deep_read.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--per-category-min", type=int, default=2)
    ap.add_argument("--per-category-max", type=int, default=10)
    ap.add_argument("--all", action="store_true",
                    help="consider the whole corpus, not just today's fresh items. "
                         "The daily runner passes this: a fresh-only plan describes at "
                         "most one day of work and silently discards the backlog of "
                         "cards that were never deep-read.")
    ap.add_argument("--fresh-only", action="store_true",
                    help="explicitly restrict the plan to today's fresh items")
    ap.add_argument("--show", action="store_true", help="print the queue")
    ap.add_argument("--dry-run", action="store_true", help="do not write the plan file")
    args = ap.parse_args(argv)

    # Default to the WHOLE corpus. Fresh-only is the special case, not the norm:
    # a card that was collected last week and never deep-read is exactly the
    # backlog this queue exists to drain.
    fresh_only = bool(args.fresh_only) and not args.all
    plan = build_plan(max(1, args.per_category_min), max(1, args.per_category_max),
                      fresh_only=fresh_only)

    t = plan["totals"]
    print(f"corpus={t['corpus']} candidates={t['candidates']} "
          f"skimmed={t['skimmed']} selected={t['selected']} over {t['categories']} categories")
    print("noise filtered by rule: " + (json.dumps(plan["skimByRule"], ensure_ascii=False) or "{}"))
    print("")
    print(f"{'category':<14}{'quota':>6}{'picked':>7}{'candidates':>11}  weight")
    for cat, info in sorted(plan["perCategory"].items(), key=lambda kv: -kv[1]["picked"]):
        marker = "  <-- under quota" if info["picked"] < info["quota"] else ""
        print(f"{cat:<14}{info['quota']:>6}{info['picked']:>7}{info['candidates']:>11}"
              f"  {info['categoryWeight']:.2f}{marker}")

    if args.show:
        print("")
        print("--- deep-read queue (top 25) ---")
        for row in plan["queue"][:25]:
            print(f"  {row['depthScore']:>6}  {row['category']:<13} {str(row['title'])[:62]}")

    if args.dry_run:
        print("\n--dry-run: plan not written")
        return 0

    with io.open(PLAN, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=1)
    print(f"\nwrote {PLAN} ({PLAN.stat().st_size} bytes)")
    print("the daily agent layer reads this file as its work list "
          "(scripts/daily-agent.md)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
