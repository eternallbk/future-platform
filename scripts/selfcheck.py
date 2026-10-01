#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selfcheck.py - integrity / authenticity gate for the Future workbench data layer.
================================================================================
Run after every collection. It answers one question: "can the workbench trust
what is on disk right now?"

Checks
  A. Every JSON file under web/data parses (catches a half-written file).
  B. Required artifacts exist: manifest.json, items/index.json, digest/today.json,
     sources.json, taxonomy.json.
  C. Index consistency: unique ids, every item has a source URL, a category
     drawn from the taxonomy, and a plausible relevance score.
  D. Cross-file consistency: manifest.totalItems matches the index size;
     digest item ids are a subset of the index (no orphans).
  E. Authenticity signals: share of items with an empty summary, share of items
     whose URL host is not a known source host (possible junk), duplicate rate.
  F. Staleness: how long since the last successful run, and whether any channel
     has been failing for 3+ consecutive runs (the alert condition).

Exit codes
  0 = all checks pass
  2 = warnings only (data is usable, but something needs human attention)
  1 = errors (data is not trustworthy / missing)

Usage
  python scripts/selfcheck.py            # full report
  python scripts/selfcheck.py --status   # compact status for the console
  python scripts/selfcheck.py --summary  # one-line summary for logs
  python scripts/selfcheck.py --json     # machine-readable
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "web" / "data"
CST = timezone(timedelta(hours=8))

REQUIRED = [
    "manifest.json",
    "items/index.json",
    "digest/today.json",
    "sources.json",
    "taxonomy.json",
]

# Hosts we expect to see. Anything else is reported (not rejected) so that a
# mis-parsed page cannot silently pollute the knowledge base.
KNOWN_HOSTS = {
    "arxiv.org", "huggingface.co", "github.com", "openreview.net",
    "semanticscholar.org", "news.ycombinator.com", "reddit.com",
    "jiqizhixin.com", "qbitai.com", "openai.com", "rsshub.app",
    "nowcoder.com", "zhihu.com", "jobs.bytedance.com", "careers.tencent.com",
    "talent.alibaba.com", "zhipuai.cn", "moonshot.cn", "deepseek.com",
    "minimaxi.com", "shlab.org.cn", "aclanthology.org", "paperswithcode.com",
    "leetcode.cn", "codeforces.com", "youtube.com", "bilibili.com",
}


class Report:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.info: list[str] = []
        self.metrics: dict = {}

    def err(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def ok(self, msg):
        self.info.append(msg)


def load(path: Path, rep: Report):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as exc:
        rep.err(f"{path.relative_to(DATA)}: unparsable ({type(exc).__name__}: {exc})")
        return None


def check_all_json(rep: Report):
    bad = 0
    total = 0
    for p in sorted(DATA.rglob("*.json")):
        total += 1
        try:
            with open(p, encoding="utf-8") as fh:
                json.load(fh)
        except Exception as exc:
            bad += 1
            rep.err(f"{p.relative_to(DATA)}: {type(exc).__name__} {exc}")
    rep.metrics["jsonFiles"] = total
    if total and not bad:
        rep.ok(f"{total} JSON files parse cleanly")


def check_required(rep: Report):
    for rel in REQUIRED:
        p = DATA / rel
        if not p.exists():
            rep.err(f"missing required artifact: {rel}")
        elif p.stat().st_size < 3:
            rep.err(f"required artifact is empty: {rel}")
        else:
            rep.ok(f"{rel} present ({p.stat().st_size / 1024:.1f} KiB)")


def check_index(rep: Report, index, taxonomy, manifest):
    if not index:
        return
    items = index.get("items") or []
    rep.metrics["totalItems"] = len(items)
    if not items:
        rep.warn("items/index.json contains no items - has the collector ever succeeded?")
        return

    ids = [i.get("id") for i in items]
    dupes = [k for k, n in Counter(ids).items() if n > 1]
    if dupes:
        rep.err(f"{len(dupes)} duplicate item ids, e.g. {dupes[:3]}")
    else:
        rep.ok(f"{len(items)} items, all ids unique")

    cat_ids = {c.get("id") for c in (taxonomy or {}).get("categories", [])}
    no_url = [i for i in items if not i.get("url")]
    no_sum = [i for i in items if not (i.get("summary") or "").strip()]
    no_cat = [i for i in items if not i.get("category")]
    bad_cat = [i for i in items if cat_ids and i.get("category") not in cat_ids]
    bad_rel = [i for i in items if not isinstance(i.get("relevanceScore"), (int, float))]

    if no_url:
        rep.err(f"{len(no_url)} items have no source URL (they must be dropped at ingest)")
    if no_cat:
        rep.err(f"{len(no_cat)} items have no category")
    if bad_cat:
        rep.warn(f"{len(bad_cat)} items use a category not in taxonomy.json, e.g. "
                 f"{sorted({i.get('category') for i in bad_cat})[:5]}")
    if bad_rel:
        rep.warn(f"{len(bad_rel)} items have no numeric relevanceScore")
    sum_pct = 100.0 * (len(items) - len(no_sum)) / len(items)
    if sum_pct < 80:
        rep.warn(f"only {sum_pct:.0f}% of items carry a summary (target >= 80%)")
    else:
        rep.ok(f"{sum_pct:.0f}% of items carry a summary")

    hosts = Counter()
    unknown = Counter()
    for i in items:
        h = urlsplit(i.get("url") or "").netloc.lower().replace("www.", "")
        if not h:
            continue
        hosts[h] += 1
        if not any(h == k or h.endswith("." + k) for k in KNOWN_HOSTS):
            unknown[h] += 1
    rep.metrics["topHosts"] = hosts.most_common(8)
    if unknown:
        share = sum(unknown.values()) / max(1, len(items))
        msg = (f"{len(unknown)} source hosts are outside the known-host list "
               f"({share * 100:.0f}% of items), e.g. {unknown.most_common(4)}")
        (rep.warn if share > 0.15 else rep.ok)(msg)
    else:
        rep.ok("every source host is a recognised channel host")

    if manifest and manifest.get("totalItems") is not None:
        if int(manifest["totalItems"]) != len(items):
            rep.warn(f"manifest.totalItems={manifest['totalItems']} but index has {len(items)} items")
        else:
            rep.ok(f"manifest.totalItems matches the index ({len(items)})")

    # digest <-> index consistency
    digest = None
    p = DATA / "digest" / "today.json"
    if p.exists():
        try:
            digest = json.loads(p.read_text("utf-8"))
        except Exception:
            digest = None
    if digest:
        idset = set(ids)
        fresh = digest.get("items") or []
        orphans = [i.get("id") for i in fresh if i.get("id") not in idset]
        if orphans:
            rep.warn(f"{len(orphans)} digest items are absent from the index (orphans)")
        else:
            rep.ok(f"digest/today.json is a consistent subset ({len(fresh)} fresh items)")

    cats = Counter(i.get("category") for i in items)
    chans = Counter(i.get("channel") for i in items)
    rep.metrics["byCategory"] = dict(cats.most_common())
    rep.metrics["byChannel"] = dict(chans.most_common(12))
    if cat_ids:
        uncovered = sorted(cat_ids - set(cats))
        if uncovered:
            rep.info.append(f"categories with no items yet: {', '.join(uncovered)}")


def check_runs(rep: Report, manifest):
    runs = None
    p = DATA / "logs" / "runs.json"
    try:
        if p.exists():
            runs = json.loads(p.read_text("utf-8"))
    except Exception as exc:
        rep.err(f"logs/runs.json unparsable: {exc}")
        return
    if not isinstance(runs, list) or not runs:
        rep.warn("no run history recorded yet")
        return
    rep.metrics["runs"] = len(runs)
    last = runs[0]
    when = last.get("startedAt")
    rep.ok(f"last run {when} status={last.get('status')} new={last.get('newItems')} "
           f"channels={last.get('channelsOk')}/{last.get('channelsTotal')}")

    if when:
        try:
            age = datetime.now(CST) - datetime.fromisoformat(when)
            hours = age.total_seconds() / 3600
            rep.metrics["hoursSinceLastRun"] = round(hours, 1)
            if hours > 36:
                rep.warn(f"last run was {hours:.0f}h ago - the 20:00 schedule may be failing")
            else:
                rep.ok(f"last run was {hours:.1f}h ago")
        except Exception:
            pass

    # channels failing 3+ consecutive runs -> alert (per the reliability spec)
    fails: Counter = Counter()
    for r in runs[:6]:
        for part in str(r.get("notes") or "").split(";"):
            part = part.strip()
            m = re.match(r"^([\w\-]+)=(error|blocked|empty)$", part)
            if m and m.group(2) in ("error", "blocked"):
                fails[m.group(1)] += 1
    hard = {k: v for k, v in fails.items() if v >= 3}
    if hard:
        rep.warn(f"channels failing for 3+ consecutive runs: {hard} -> needs human review "
                 f"(see web/data/proposals/)")
    rep.metrics["channelFailureStreaks"] = dict(fails)


def check_proposals(rep: Report):
    p = DATA / "proposals"
    if not p.exists():
        rep.info.append("no proposals directory yet (created by the agent deep-read layer)")
        return
    files = sorted(p.glob("*.json"))
    if not files:
        rep.info.append("no proposals queued for human review")
    else:
        rep.ok(f"{len(files)} proposal file(s) waiting for human review: "
               f"{', '.join(f.name for f in files[-3:])}")


def build_report() -> Report:
    rep = Report()
    check_all_json(rep)
    check_required(rep)
    manifest = load(DATA / "manifest.json", rep) or {}
    index = load(DATA / "items" / "index.json", rep)
    taxonomy = load(DATA / "taxonomy.json", rep)
    check_index(rep, index, taxonomy, manifest)
    check_runs(rep, manifest)
    check_proposals(rep)
    rep.metrics["generatedAt"] = datetime.now(CST).isoformat(timespec="seconds")
    return rep


def print_report(rep: Report):
    print("")
    print("=" * 72)
    print("Future 工作台 · 数据层自检报告")
    print("=" * 72)
    for line in rep.info:
        print(f"  [ok]   {line}")
    for line in rep.warnings:
        print(f"  [warn] {line}")
    for line in rep.errors:
        print(f"  [ERR]  {line}")
    print("-" * 72)
    print(f"  结论: {len(rep.info)} 项通过, {len(rep.warnings)} 项警告, {len(rep.errors)} 项错误")
    m = rep.metrics
    if m.get("byCategory"):
        top = list(m["byCategory"].items())[:6]
        print("  分类分布: " + ", ".join(f"{k}={v}" for k, v in top))
    if m.get("byChannel"):
        top = list(m["byChannel"].items())[:6]
        print("  渠道分布: " + ", ".join(f"{k}={v}" for k, v in top))
    if m.get("topHosts"):
        print("  主要域名: " + ", ".join(f"{h}({n})" for h, n in m["topHosts"][:5]))
    print("=" * 72)
    print("")


def print_status(rep: Report):
    m = rep.metrics
    print("")
    print("── Future 工作台 · 数据层状态 ─────────────────────────────")
    print(f"  条目总数      : {m.get('totalItems', 0)}")
    print(f"  运行历史      : {m.get('runs', 0)} 次")
    if m.get("hoursSinceLastRun") is not None:
        print(f"  距上次运行    : {m['hoursSinceLastRun']:.1f} 小时")
    print(f"  自检结论      : {len(rep.info)} 通过 / {len(rep.warnings)} 警告 / {len(rep.errors)} 错误")
    if m.get("byCategory"):
        print("  分类分布      : " + ", ".join(f"{k}={v}" for k, v in list(m['byCategory'].items())[:8]))
    for line in rep.errors:
        print(f"  [ERR]  {line}")
    for line in rep.warnings[:6]:
        print(f"  [warn] {line}")
    print("──────────────────────────────────────────────────────────")
    print("")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="selfcheck.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true", help="compact console status")
    ap.add_argument("--summary", action="store_true", help="one-line summary for the log")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    rep = build_report()

    if args.json:
        print(json.dumps({"errors": rep.errors, "warnings": rep.warnings,
                          "info": rep.info, "metrics": rep.metrics},
                         ensure_ascii=False, indent=2))
    elif args.summary:
        ok = len(rep.info); w = len(rep.warnings); e = len(rep.errors)
        print(f"[selfcheck] items={rep.metrics.get('totalItems', 0)} "
              f"passed={ok} warnings={w} errors={e}")
        for line in rep.errors[:3]:
            print(f"[selfcheck][ERR] {line}")
    elif args.status:
        print_status(rep)
    else:
        print_report(rep)

    return 1 if rep.errors else (2 if rep.warnings else 0)


if __name__ == "__main__":
    sys.exit(main())
