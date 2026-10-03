#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Assemble ``scripts/agent-input-<date>.json`` from the per-batch deep-read output.

Middle of the layer-2 pipeline (see ``scripts/daily-agent.md``)::

    out-<batch>.json  ->  agent-input-<date>.json  (this script)
                      ->  web/data/*.json          (scripts/apply_enrichment.py)

Rules it enforces:

* ``full`` batches are passed through unchanged.
* ``backfill`` batches only carry the layers named in ``missing``; they are
  **deep-merged into the existing** ``web/data/enrichment.json`` entry so that
  ``apply_enrichment.py`` (which replaces a whole byId entry) cannot drop
  already-published解析.  Deep merge = dicts recurse, everything else new-wins.
* Nothing is invented here: ids that are not in the corpus, duplicate ids, and
  missing batches are reported as errors and abort the build.

Usage
-----
    python scripts/build_agent_payload.py --date 2026-10-03
    python scripts/build_agent_payload.py --date 2026-10-03 --allow-missing
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_DIR = os.path.join(ROOT, "web", "data")


def now_iso():
    return _dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, obj):
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def deep_merge(old, new, path=""):
    """new wins; dicts merge recursively; lists replace."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = dict(old)
        for k, v in new.items():
            out[k] = deep_merge(old.get(k), v, "%s.%s" % (path, k))
        return out
    return new


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", default=_dt.date.today().isoformat())
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--out", default=None, help="default scripts/agent-input-<date>.json")
    ap.add_argument("--allow-missing", action="store_true",
                    help="warn instead of aborting when a batch output is absent")
    args = ap.parse_args(argv)

    work_dir = args.work_dir or os.path.join(ROOT, "scripts", "agent-work-%s" % args.date)
    data_dir = args.data_dir if os.path.isabs(args.data_dir) else os.path.join(ROOT, args.data_dir)
    out_path = args.out or os.path.join(ROOT, "scripts", "agent-input-%s.json" % args.date)

    manifest = read_json(os.path.join(work_dir, "manifest.json"))
    if not manifest:
        raise SystemExit("manifest not found under %s" % work_dir)
    index = read_json(os.path.join(data_dir, "items", "index.json"), {}) or {}
    known_ids = {i["id"] for i in index.get("items", []) if isinstance(i, dict)}
    old_by_id = (read_json(os.path.join(data_dir, "enrichment.json"), {}) or {}).get("byId") or {}

    enrichment, problems, stats = [], [], {"full": 0, "backfill": 0, "merged_layers": {}}
    seen_ids = set()

    for batch in manifest.get("batches", []):
        bid, mode = batch["batchId"], batch["mode"]
        out_file = os.path.join(work_dir, batch["output"])
        if not os.path.exists(out_file):
            problems.append("缺少批次输出 %s" % batch["output"])
            continue
        doc = read_json(out_file)
        entries = (doc or {}).get("entries")
        if not isinstance(entries, list):
            problems.append("%s 的 entries 不是数组" % batch["output"])
            continue
        expected = set(batch.get("ids") or [])
        got = {e.get("id") for e in entries if isinstance(e, dict)}
        if expected - got:
            problems.append("%s 缺 id：%s" % (bid, sorted(expected - got)))
        for eid in got - expected:
            problems.append("%s 多出 id：%s" % (bid, eid))
        for entry in entries:
            if not isinstance(entry, dict):
                problems.append("%s 有非对象 entry" % bid)
                continue
            eid = entry.get("id")
            if eid not in known_ids:
                problems.append("%s/%s 不在 items/index.json 里" % (bid, eid))
                continue
            if eid in seen_ids:
                problems.append("id 重复出现：%s" % eid)
                continue
            seen_ids.add(eid)
            if mode == "backfill":
                base = old_by_id.get(eid)
                if not isinstance(base, dict):
                    problems.append("%s/%s 声称回填但 enrichment.json 里没有旧值" % (bid, eid))
                    continue
                merged = deep_merge(base, entry)
                for k in entry:
                    if k != "id":
                        stats["merged_layers"][k] = stats["merged_layers"].get(k, 0) + 1
                enrichment.append(merged)
                stats["backfill"] += 1
            else:
                enrichment.append(entry)
                stats["full"] += 1

    for p in problems:
        print("ERROR %s" % p)
    if problems and not args.allow_missing:
        print("build aborted: %d problem(s)" % len(problems))
        return 1

    plan = read_json(os.path.join(data_dir, "deep-read-plan.json"), {}) or {}

    # ---- 题库定位题解批次 (out-prob-*.json) ------------------------------------
    #
    # The problem queue is a SEPARATE work list with its own output shape (a 题解, not a
    # knowledge card), so its batches are read separately. Missing problem output is a
    # WARNING instead of an error, deliberately: the deep-read layer is optional by
    # contract (`docs/05`), and a day where the agent ran out of budget must still publish
    # the deterministic data. The gap is visible in the log's problemsProcessed/queued.
    prob_batches = sorted(glob.glob(os.path.join(work_dir, "out-prob-*.json")))
    planned_problems = {str(p.get("id")): p for p in (plan.get("problems") or [])}
    # The accepted-id set must be the WHOLE bank, not just this run's queue.
    #
    # MEASURED BUG: filtering entries against `plan.problems` alone silently dropped 15 of
    # 22 written 题解, because the plan contains only the still-un-analysed problems - so a
    # solution written for a curated 手撕题 (or for a collected problem that left the queue
    # between planning and writing) was reported "不在本轮题库队列里" and thrown away. Work
    # that has already been produced must never be discarded by a bookkeeping check.
    bank = read_json(os.path.join(data_dir, "problem-bank.json"), {}) or {}
    known_problems = set(planned_problems)
    for p in bank.get("problems") or []:
        if isinstance(p, dict) and p.get("id"):
            known_problems.add(str(p["id"]))
    for key in ("curated", "curatedAnalyzed"):
        for c in bank.get(key) or []:
            if isinstance(c, dict) and c.get("id"):
                known_problems.add(str(c["id"]))
    problem_analyses, seen_problems = [], set()
    for path in prob_batches:
        doc = read_json(path) or {}
        entries = doc.get("entries")
        if not isinstance(entries, list):
            print("WARN %s 的 entries 不是数组" % os.path.basename(path))
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                print("WARN %s 有非对象 entry" % os.path.basename(path))
                continue
            pid = str(entry.get("id") or "")
            if pid not in known_problems:
                print("WARN %s/%s 不在题库里（采集题与人工题都不是）" % (os.path.basename(path), pid))
                continue
            if pid in seen_problems:
                print("WARN id 重复出现：%s" % pid)
                continue
            seen_problems.add(pid)
            problem_analyses.append(entry)
    if planned_problems and not prob_batches:
        print("WARN 题库队列有 %d 题，但没有 out-prob-*.json（本轮未产出题解）"
              % len(planned_problems))

    jobs_doc = read_json(os.path.join(work_dir, "jobs.json"), []) or []
    jobs = jobs_doc.get("jobs") if isinstance(jobs_doc, dict) else jobs_doc
    proposals = read_json(os.path.join(work_dir, "proposals.json"), {}) or {}
    log = read_json(os.path.join(work_dir, "log.json"), {}) or {}
    skips = read_json(os.path.join(work_dir, "skips.json"), {}) or {}

    skip_by_rule = {}
    for s in skips.get("skips", []):
        skip_by_rule[s.get("rule")] = skip_by_rule.get(s.get("rule"), 0) + 1

    missing_sets = {}
    for b in plan.get("backfill", []):
        key = "+".join(sorted(b.get("missing") or [])) or "none"
        missing_sets[key] = missing_sets.get(key, 0) + 1

    selection = {
        "basis": "web/data/deep-read-plan.json 的 queue + backfill（scripts/plan_deep_read.py 生成：质量闸门 + 每类 2–10 条配额）",
        "readFrom": "web/data/deep-read-plan.json",
        "processing": "%d 个批次由并行子代理处理，各自写 scripts/agent-work-%s/out-<batchId>.json；再由 scripts/build_agent_payload.py 合并成 payload，scripts/validate_agent_out.py 做硬校验（schema、图解、URL 来源、teaching 十项）" % (len(manifest.get("batches", [])), args.date),
        "planGeneratedAt": plan.get("generatedAt"),
        "fullQueueSize": len(plan.get("queue", [])),
        "queueProcessed": stats["full"],
        "queueSkipped": len(skips.get("skips", [])),
        "skipReasons": "；".join("%s %d 条" % (k, v) for k, v in sorted(skip_by_rule.items())),
        "skipsFile": "scripts/agent-work-%s/skips.json" % args.date,
        "backfillTotal": len(plan.get("backfill", [])),
        "backfillProcessed": stats["backfill"],
        "backfillMissingSets": missing_sets,
        "backfillMergedLayers": stats["merged_layers"],
        "batches": [{"batchId": b["batchId"], "mode": b["mode"], "count": b["count"]}
                    for b in manifest.get("batches", [])],
        "corpusSize": len(known_ids),
        "alreadyEnrichedBeforeThisRun": len(old_by_id),
        "problemsQueued": len(planned_problems),
        "problemsProcessed": len(problem_analyses),
        "problemsFiles": [os.path.basename(p) for p in prob_batches],
        "problemsNote": "题库定位的题解由 out-prob-*.json 单独产出（题目→思路/复杂度/代码/配图/易错点/追问），"
                        "与卡片深读互不重复：排入题库队列的条目已从卡片队列中移除。",
        "quotaRule": "完全按 deep-read-plan.json 的清单执行，不自己另挑条目；唯一的人工干预是按证据剔除误分类/占位条目，依据写在 skips.json，可复核、可回滚。",
        "note": "backfill 条目只补 missing 里缺的层，由本脚本深合并进已有 enrichment，绝不重写或删除已有解析。",
    }

    payload = {
        "date": args.date,
        "generatedAt": now_iso(),
        "generatedBy": "scripts/build_agent_payload.py",
        "selection": selection,
        "enrichment": enrichment,
        "problemAnalyses": problem_analyses,
        "jobs": jobs or [],
        "proposals": proposals,
        "log": log,
    }
    write_json(out_path, payload)

    print("work dir  : %s" % work_dir)
    print("payload   : %s" % out_path)
    print("enrichment: full %d + backfill %d = %d" % (stats["full"], stats["backfill"], len(enrichment)))
    print("merged layers (backfill): %s" % json.dumps(stats["merged_layers"], ensure_ascii=False))
    print("jobs      : %d" % len(jobs or []))
    print("proposals : %s" % ("yes" if proposals else "none"))
    print("题解      : %d/%d 题（题库队列）" % (len(problem_analyses), len(planned_problems)))
    print("problems  : %d" % len(problems))
    return 0


if __name__ == "__main__":
    sys.exit(main())
