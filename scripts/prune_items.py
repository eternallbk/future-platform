#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prune_items.py — 存量知识卡片的解析自查与清理

要解决的问题
  采集器的 `minRelevance` 只有 12 分（满分 100），目的是「宁滥勿缺」。代价是库里
  积累了一批**对算法实习没有指导价值**的卡片：求职吐槽、泛化提问、内推码、
  与技术无关的社区热榜、只有标题没有内容的条目。它们不会出现在搜索的前排，
  但会拉低「今日速览」的信噪比，也让「每类都要有深度解析」变得不可达。

它做什么
  A. **自查**：给每张卡片打一个 `importance` 分（0–100），并归入三档：
       core    —— 方向内的论文/仓库/课程/手撕题/正式岗位信息（保留）
       useful  —— 有信息量的招聘、面经、行业动态（保留）
       noise   —— 无指导价值（建议下架）
     同时做**近似重复检测**：同一标题骨架出现多条时，只留最完整的那条。
  B. **优化**：默认只**报告**（dry-run）。要真正下架需显式 `--apply`。
     下架方式默认是 `--mode archive`：从 `items/index.json` 移出、写入
     `web/data/archive/pruned-YYYY-MM-DD.json` 并登记进 state 的 blocklist，
     这样**采集器不会再把它收回来**（否则下次采集立刻复活）。
     `--mode delete` 直接丢弃（不推荐，无法回溯）。

为什么默认 archive 而不是 delete
  下架决策是主观的，将来可能想找回来；但如果不写 blocklist，下一轮采集就会
  把同一条目重新收进来，下架等于白做。archive + blocklist 两者都满足。

用法
  python scripts/prune_items.py                      # 只报告
  python scripts/prune_items.py --show 30            # 报告并列出下架明细
  python scripts/prune_items.py --apply              # 执行（archive 模式）
  python scripts/prune_items.py --apply --mode delete
  python scripts/prune_items.py --min-keep-score 30  # 提高保留门槛
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "web" / "data"
CST = timezone(timedelta(hours=8))

# The commerce/junk judgement lives in collect.py, because the collector is where the
# decision has to be made for NEW items. Importing it here (instead of copying the regexes)
# is what keeps the cleaner and the ingester from drifting apart - the failure mode this
# avoids is real: prune_items.py removed 165 items once, the blocklist was not consulted by
# the collector, and the whole set came back the next day.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import collect as _collect  # type: ignore
except Exception:  # noqa: BLE001
    _collect = None

INDEX = DATA / "items" / "index.json"
ITEMS_COPY = DATA / "items.json"
STATE = DATA / "state" / "collector-state.json"
ARCHIVE_DIR = DATA / "archive"

# Channels whose items are analysis material by construction.
MATERIAL_CHANNELS = {"arxiv", "openreview", "hf_papers", "s2", "hf_blog_rss",
                     "openai_rss", "github", "gh_trending"}
# Categories that carry technical content.
MATERIAL_CATEGORIES = {"multimodal", "posttraining", "worldmodel", "generative",
                       "rl", "coding", "foundation", "engineering", "agent",
                       "paper", "course", "exam"}

# Noise rules (same spirit as plan_deep_read.py, but these act on the CORPUS,
# not only on the deep-read queue: a card that never deserves analysis should not
# sit in the library either).
NOISE_RULES = [
    ("career-vent", "求职吐槽 / 情绪帖，无信息量",
     [r"处境.*未来", r"勤勤恳恳", r"不摸鱼", r"升职加薪.*轮不到", r"要不要跑",
      r"劝退", r"后悔", r"焦虑", r"破防", r"内卷.*值得", r"值不值得.*主攻"]),
    ("generic-question", "泛化提问，无法结构化成知识",
     [r"^为什么很多人", r"^如何评价", r"^大家怎么看", r"^有人知道吗", r"^请问",
      r"^求推荐", r"^求问", r"^如何看待"]),
    ("referral-code", "内推码 / 广告，只需进岗位信息",
     [r"内推码", r"内推链接", r"扫.*码.*投递", r"急招.*加微信"]),
    ("salary-thread", "纯薪资讨论，数字无法核实且易重复",
     [r"薪资.*爆料", r"offer.*对比.*薪资", r"总包.*多少", r"薪资倒挂"]),
    ("off-topic-hot", "与技术无关的社区热榜",
     [r"土豆", r"减肥", r"转专业", r"永动机", r"食堂", r"宿舍", r"考研英语",
      r"公务员", r"考公", r"相亲", r"房价", r"裁员潮.*怎么办"]),
]


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def load(path: Path, fallback):
    try:
        if path.exists():
            return json.loads(path.read_text("utf-8"))
    except Exception as e:
        print(f"[warn] {path.name}: {e}", file=sys.stderr)
    return fallback


def title_key(t: str) -> str:
    """Loose skeleton of a title, for near-duplicate detection."""
    s = re.sub(r"[\s\u3000]+", "", str(t or "").lower())
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", s)
    return s


def _is_question_item(item: dict) -> bool:
    """Is this card a concrete QUESTION (rather than an article/repo about a topic)?"""
    if item.get("problemKind"):
        return True
    if str(item.get("channel") or "") == "nowcoder_questions":
        return True
    return bool(re.match(r"\s*\[(编程题|问答题|单选题|多选题)\]", str(item.get("title") or "")))


def noise_reason(item: dict) -> tuple[str, str] | None:
    title = str(item.get("title") or "")
    summary = str(item.get("summary") or "")
    blob = f"{title} {summary}"
    # 1. 商业/垃圾内容：与采集层用同一套判定（collect.junk_reason）。
    #    这一条是读者投诉的直接落点：「API 订阅指南、充值教程、卖课」。
    if _collect is not None:
        try:
            why = _collect.junk_reason(item)
        except Exception:  # noqa: BLE001
            why = ""
        if why:
            return "commerce-junk", why
    # 2. 数字电路/硬件设计题：真实题目，但与算法岗求职无关。
    #
    #    SCOPE MATTERS HERE (measured false positives, both removed by this fix):
    #      · 全库匹配 SUM 摘要 was far too broad — 「自旋锁与互斥锁对比」和「C++智能指针实现原理」
    #        were flagged because their explanations mention 寄存器/状态转移;
    #      · a CUDA kernel article was flagged for the same reason (kernels do talk about
    #        registers).
    #    The rule was written for 牛客 question pages, where the hardware term is IN THE
    #    TITLE ("[编程题] 用3-8译码器实现全减器"). So it now applies only to items that are
    #    actually questions, and only against the title.
    if _collect is not None and _is_question_item(item) \
            and _collect.PROBLEM_EXCLUDE_RE.search(title):
        return ("hardware-exercise",
                "数字电路/硬件设计题（FPGA/编码器/触发器），与算法岗面试无关")
    for rid, why, patterns in NOISE_RULES:
        for p in patterns:
            if re.search(p, blob):
                if rid == "referral-code" and len(summary) > 200:
                    continue        # a real JD that happens to mention a code
                return rid, why
    return None


def channel_quality_floor(item: dict) -> tuple[str, str] | None:
    """Channel-specific quality floors, mirroring the collectors' own gates.

    Why this exists separately from the collector: a rule that only lives in the
    collector cannot clean the corpus ingested BEFORE the rule existed. The audit
    found 80 low-value cards from hf_models (random community uploads, duplicate
    `-MLX`/`-NVF` re-uploads) that were collected before the popularity floor was
    added. Keep these thresholds in sync with collect.py.

    Note: older hf_models cards stored the counts in the summary TEXT rather than
    in qualitySignals, so both places are read.
    """
    ch = str(item.get("channel") or "")
    qs = item.get("qualitySignals") or {}
    if ch == "hf_models":
        dl = qs.get("downloads")
        likes = qs.get("likes")
        if dl is None or likes is None:
            m = re.search(r"downloads=(\d+).*?likes=(\d+)", str(item.get("summary") or ""))
            if not m:
                m = re.search(r"下载\s*(\d+)[^\d]+点赞\s*(\d+)", str(item.get("summary") or ""))
            if m:
                dl = int(m.group(1))
                likes = int(m.group(2))
        dl = int(dl or 0)
        likes = int(likes or 0)
        if dl < 2000 and likes < 15:
            return ("hf-model-low-popularity",
                    f"HuggingFace 社区低人气模型（下载 {dl} / 点赞 {likes}），"
                    f"多为个人上传，没有领域信号")
    return None


def score_item(item: dict) -> tuple[int, str, list[str]]:
    """Return (importance 0-100, tier, reasons)."""
    reasons: list[str] = []
    rel = float(item.get("relevanceScore") or 0)
    cat = str(item.get("category") or "trend")
    ch = str(item.get("channel") or "")
    summary = str(item.get("summary") or "").strip()
    title = str(item.get("title") or "").strip()

    floor = channel_quality_floor(item)
    if floor:
        reasons.append(f"命中渠道质量门槛({floor[0]})")
        return 0, "noise", reasons

    score = rel * 0.45
    reasons.append(f"相关度 {rel:.0f} ×0.45")

    if cat in MATERIAL_CATEGORIES:
        score += 18
        reasons.append("技术分类 +18")
    else:
        reasons.append(f"非技术分类({cat}) +0")

    if ch in MATERIAL_CHANNELS:
        score += 14
        reasons.append(f"一手来源({ch}) +14")

    if item.get("peerReviewed"):
        score += 8
        reasons.append("同行评审 +8")
    if item.get("codeAvailable"):
        score += 6
        reasons.append("有开源实现 +6")
    if item.get("ccf"):
        score += 5
        reasons.append(f"CCF-{item['ccf']} +5")

    # Content depth: a card with no summary is a link, not a card.
    if len(summary) >= 400:
        score += 8
        reasons.append("摘要充实 +8")
    elif len(summary) >= 120:
        score += 4
        reasons.append("摘要可用 +4")
    elif not summary:
        # A card with no explanation at all cannot be deep-read, cannot be
        # scored for relevance meaningfully, and cannot teach anything. The
        # collector tries to backfill these from the page's meta description; if
        # that failed too (older S2 records with no abstract, HF blog entries
        # older than the feed window) the item is not knowledge, it is a
        # bookmarked URL. Push it firmly into the noise tier.
        score -= 55
        reasons.append("完全没有摘要，无法深读 -55")
    elif len(summary) < 80:
        score -= 10
        reasons.append("摘要过短 -10")

    if item.get("enriched"):
        score += 10
        reasons.append("已有深度解析 +10")

    if len(title) < 14:
        score -= 8
        reasons.append("标题过短 -8")

    hit = noise_reason(item)
    if hit:
        score -= 45
        reasons.append(f"命中噪音规则({hit[0]}) -45")

    score = max(0, min(100, round(score)))

    if hit:
        tier = "noise"
    elif not summary:
        # No explanation => not a knowledge card, whatever its channel.
        tier = "noise"
    elif cat in MATERIAL_CATEGORIES and (ch in MATERIAL_CHANNELS or item.get("enriched")):
        tier = "core"
    elif cat in ("job", "trend", "exam", "course") or ch in ("jobs_tencent", "jobs_deepseek",
                                                            "nowcoder", "zhihu", "inbox"):
        tier = "useful"
    else:
        tier = "useful" if score >= 40 else "noise"
    return score, tier, reasons


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="prune_items.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually prune (default: report only)")
    ap.add_argument("--mode", choices=["archive", "delete"], default="archive")
    ap.add_argument("--min-keep-score", type=int, default=25,
                    help="cards below this importance are candidates for removal (default 25)")
    ap.add_argument("--keep-jobs", action="store_true", default=True,
                    help="never prune items whose category is job (default on)")
    ap.add_argument("--prune-jobs", dest="keep_jobs", action="store_false")
    ap.add_argument("--show", type=int, default=0, help="print this many removal candidates")
    ap.add_argument("--ids-file", default="",
                    help="JSON produced by audit_corpus.py --json; its `drop[]` ids are "
                         "removed exactly, bypassing this script's own heuristics")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    index = load(INDEX, {}) or {}
    items = index.get("items") or []
    if not items:
        print("items/index.json is empty; nothing to do")
        return 0

    # --- explicit removal list (from the corpus audit) ----------------------
    # WHY this mode exists: this script's heuristics are broad (importance score,
    # near-duplicates, noise rules) and are meant for bulk housekeeping. Judging
    # individual items - "is THIS paper about a topic the workbench tracks?" - needs
    # reading the item, which audit_corpus.py does. `--ids-file` lets that judgement
    # drive the removal while still going through THIS script's blocklist machinery,
    # so the removal is recorded and cannot be undone by the next collection.
    explicit_ids: set[str] = set()
    if args.ids_file:
        report = load(Path(args.ids_file), {}) or {}
        for row in (report.get("drop") or []):
            if row.get("id"):
                explicit_ids.add(row["id"])
        if not explicit_ids:
            print(f"  {args.ids_file}: no `drop[].id` entries found")
        else:
            print(f"  从 {args.ids_file} 读取到 {len(explicit_ids)} 条待下架 id")

    state = load(STATE, {}) or {}
    blocked_ids = set(state.get("blockedIds") or [])
    blocked_titles = set(state.get("blockedTitleKeys") or [])
    enrich = (load(DATA / "enrichment.json", {}) or {}).get("byId") or {}

    for it in items:
        it["enriched"] = it.get("id") in enrich

    scored = []
    for it in items:
        s, tier, why = score_item(it)
        scored.append({"item": it, "score": s, "tier": tier, "reasons": why,
                       "noise": noise_reason(it)})

    # --- near-duplicate detection -----------------------------------------
    by_key: dict[str, list] = {}
    for row in scored:
        by_key.setdefault(title_key(row["item"].get("title")), []).append(row)
    dup_drop = []
    for key, rows in by_key.items():
        if not key or len(rows) < 2:
            continue
        rows.sort(key=lambda r: (-r["score"], -(len(str(r["item"].get("summary") or "")))))
        for r in rows[1:]:
            dup_drop.append({**r, "dupOf": rows[0]["item"].get("id")})

    dup_ids = {r["item"].get("id") for r in dup_drop}

    # --- removal decision --------------------------------------------------
    remove, keep = [], []
    for row in scored:
        it = row["item"]
        iid = it.get("id")
        if explicit_ids and iid in explicit_ids:
            remove.append({**row, "removeWhy": "audit_corpus.py 判定为与求职学习无关"})
            continue
        if iid in dup_ids:
            why = f"与 {next(d['dupOf'] for d in dup_drop if d['item'].get('id') == iid)} 标题近似重复"
            remove.append({**row, "removeWhy": why})
            continue
        if args.keep_jobs and it.get("category") == "job" and row["tier"] != "noise":
            keep.append(row)
            continue
        # With --ids-file the caller has named the exact items to remove, so the broad
        # heuristics stay out of the way. Without this, passing a 7-item audit list
        # would ALSO trigger the importance/noise rules and remove a much larger set -
        # a destructive surprise in a tool whose whole point is precision here.
        if explicit_ids:
            keep.append(row)
            continue
        if row["tier"] == "noise" or row["score"] < args.min_keep_score:
            remove.append({**row, "removeWhy": row["noise"][1] if row["noise"]
                           else f"重要度 {row['score']} 低于保留门槛 {args.min_keep_score}"})
        else:
            keep.append(row)

    tiers = Counter(r["tier"] for r in scored)
    rules = Counter(r["noise"][0] for r in scored if r["noise"])
    by_cat_remove = Counter(r["item"].get("category") for r in remove)

    report = {
        "generatedAt": now_iso(),
        "policy": {"minKeepScore": args.min_keep_score, "mode": args.mode,
                   "keepJobs": bool(args.keep_jobs),
                   "note": "archive 模式会写入 blocklist，避免下一轮采集把下架条目收回来"},
        "totals": {"inLibrary": len(items), "keep": len(keep), "remove": len(remove),
                   "duplicates": len(dup_drop), "alreadyBlocked": len(blocked_ids)},
        "tiers": dict(tiers),
        "noiseByRule": dict(rules),
        "removalsByCategory": dict(by_cat_remove),
        "removalsByRule": dict(Counter(r["noise"][0] for r in remove if r["noise"])),
        "samples": [{"id": r["item"].get("id"), "title": str(r["item"].get("title"))[:90],
                     "category": r["item"].get("category"), "channel": r["item"].get("channel"),
                     "score": r["score"], "tier": r["tier"], "why": r["removeWhy"]}
                    for r in remove[:40]],
        "keepSamples": [{"title": str(r["item"].get("title"))[:78], "score": r["score"],
                         "tier": r["tier"], "category": r["item"].get("category")}
                        for r in sorted(keep, key=lambda r: -r["score"])[:12]],
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    print("")
    print("=" * 78)
    print("知识卡片自查与清理" + ("（--apply：将执行）" if args.apply else "（仅报告）"))
    print("=" * 78)
    print(f"  库内卡片        : {len(items)}")
    print(f"  分档            : core={tiers.get('core', 0)}  useful={tiers.get('useful', 0)}  noise={tiers.get('noise', 0)}")
    print(f"  建议保留 / 下架 : {len(keep)} / {len(remove)}"
          f"（其中近似重复 {len(dup_drop)} 条）")
    if rules:
        print("  命中噪音规则    : " + ", ".join(f"{k}={v}" for k, v in rules.most_common()))
    if by_cat_remove:
        print("  下架按分类      : " + ", ".join(f"{k}={v}" for k, v in by_cat_remove.most_common(8)))
    print("")
    print("  —— 保留的样本（重要度最高的 12 条）——")
    for s in report["keepSamples"]:
        print(f"    {s['score']:>3} [{s['tier']:<6}] {s['category']:<13} {s['title']}")
    if args.show:
        print("")
        print(f"  —— 下架候选（前 {args.show} 条）——")
        for r in remove[:args.show]:
            print(f"    {r['score']:>3} [{r['tier']:<5}] {str(r['item'].get('category')):<12} "
                  f"{str(r['item'].get('title'))[:56]}")
            print(f"         理由：{r['removeWhy']}")
    print("=" * 78)

    if not args.apply:
        print("")
        print("这是**只报告**模式，没有改动任何数据。要执行：")
        print("  python scripts/prune_items.py --apply")
        print("")
        return 0

    # --- apply -------------------------------------------------------------
    remove_ids = {r["item"].get("id") for r in remove}
    kept_items = [it for it in items if it.get("id") not in remove_ids]

    if args.mode == "archive":
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        day = datetime.now(CST).strftime("%Y-%m-%d")
        payload = {"archivedAt": now_iso(), "count": len(remove),
                   "policy": report["policy"],
                   "items": [r["item"] for r in remove]}
        with io.open(ARCHIVE_DIR / f"pruned-{day}.json", "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        print(f"  archived {len(remove)} items -> data/archive/pruned-{day}.json")

    # Write the trimmed index (and the local copy the app also reads).
    for path in (INDEX, ITEMS_COPY):
        doc = dict(load(path, {}) or {})
        doc["items"] = kept_items
        doc["count"] = len(kept_items)
        doc["prunedAt"] = now_iso()
        doc["prunedCount"] = len(remove)
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"  index rewritten: {len(items)} -> {len(kept_items)} items")

    # manifest.totalItems must follow the index, or selfcheck reports a mismatch
    # ("manifest.totalItems=760 but index has 554 items") and the UI header lies.
    man_path = DATA / "manifest.json"
    man = dict(load(man_path, {}) or {})
    if man:
        man["totalItems"] = len(kept_items)
        man["prunedAt"] = now_iso()
        man["prunedCount"] = len(remove)
        stats = dict(man.get("stats") or {})
        by_cat: dict = {}
        for it in kept_items:
            c = it.get("category") or "trend"
            by_cat[c] = by_cat.get(c, 0) + 1
        stats["byCategory"] = by_cat
        man["stats"] = stats
        # The summary headline embeds the corpus size, so it goes stale the moment
        # items are pruned ("累计 760 条" while the index holds 554).
        summ = dict(man.get("summary") or {})
        if summ:
            counts = dict(summ.get("counts") or {})
            counts["total"] = len(kept_items)
            summ["counts"] = counts
            headline = str(summ.get("headline") or "")
            if headline:
                headline = re.sub(r"累计\s*\d+\s*条", f"累计 {len(kept_items)} 条", headline)
                summ["headline"] = headline
            man["summary"] = summ
        with io.open(man_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(man, fh, ensure_ascii=False, separators=(",", ":"))
        print(f"  manifest.totalItems -> {len(kept_items)}")

    # The day's digest still lists items we just removed, which selfcheck reports
    # as "orphans" and the 每日更新流 would render as links to nothing. Prune the
    # digest too, and recompute its summary so the counts stay truthful.
    kept_ids = {it.get("id") for it in kept_items}
    for rel in ("digest/today.json",):
        dpath = DATA / rel
        dg = load(dpath, None)
        if not isinstance(dg, dict):
            continue
        before = len(dg.get("items") or [])
        dg["items"] = [i for i in (dg.get("items") or []) if i.get("id") in kept_ids]
        dg["updated"] = [i for i in (dg.get("updated") or []) if i.get("id") in kept_ids]
        if dg.get("summary"):
            counts = dict(dg["summary"].get("counts") or {})
            counts["fresh"] = len(dg["items"])
            counts["total"] = len(kept_items)
            dg["summary"]["counts"] = counts
            dg["summary"]["topItemIds"] = [i for i in (dg["summary"].get("topItemIds") or [])
                                           if i in kept_ids]
        dg["prunedAt"] = now_iso()
        with io.open(dpath, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(dg, fh, ensure_ascii=False, separators=(",", ":"))
        day = dg.get("date")
        if day:
            dated = DATA / "digest" / f"{day}.json"
            with io.open(dated, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(dg, fh, ensure_ascii=False, separators=(",", ":"))
        if before != len(dg["items"]):
            print(f"  digest fresh items: {before} -> {len(dg['items'])} (orphans removed)")

    # Blocklist so the next collection does not re-import what we just removed.
    # Without this the prune is undone on the next 14:00 run.
    state["blockedIds"] = sorted(blocked_ids | remove_ids)
    keys = {title_key(r["item"].get("title")) for r in remove}
    state["blockedTitleKeys"] = sorted(blocked_titles | {k for k in keys if k})
    state["lastPruneAt"] = now_iso()
    with io.open(STATE, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(state, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"  blocklist updated: {len(state['blockedIds'])} ids, "
          f"{len(state['blockedTitleKeys'])} title keys")
    print("")
    print("  下一步：python scripts/selfcheck.py  （确认数据层仍然自洽）")
    print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
