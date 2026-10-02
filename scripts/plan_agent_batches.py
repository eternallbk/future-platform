#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Split ``web/data/deep-read-plan.json`` into per-batch inputs for the daily agent.

This is the front half of the layer-2 pipeline described in
``scripts/daily-agent.md``:

    deep-read-plan.json  ->  in-<batch>.json  (this script)
                         ->  out-<batch>.json (one sub-agent per batch, see INSTRUCTIONS.md)
                         ->  agent-input-<date>.json (scripts/build_agent_payload.py)
                         ->  web/data/*.json        (scripts/apply_enrichment.py)

Everything the agent is allowed to know about an item comes from this file, so a
sub-agent can never reach outside the corpus (rule 1/2 of daily-agent.md).

Two things are decided here instead of by the sub-agents, because they must be
reviewable and reproducible:

* **batching** -- how many items per output file (output size is the limit).
* **exclusions** -- items the deterministic plan misclassified.  Every exclusion
  needs a quotation-level reason in ``skips.json``; nothing is dropped silently
  and every excluded item stays searchable in the card library.

Usage
-----
    python scripts/plan_agent_batches.py --date 2026-10-03
    python scripts/plan_agent_batches.py --date 2026-10-03 --queue-per-batch 5
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_DIR = os.path.join(ROOT, "web", "data")

# --------------------------------------------------------------------------- #
# Evidence-based exclusions for the 2026-10-03 queue.
#
# The collector scores on keyword substrings, so ``multimodal`` / ``foundation``
# / ``paper`` / ``rl`` picks up neighbouring disciplines.  Rules of thumb:
#
#   off-domain  -- the title/summary shows a different field; nothing about the
#                  method transfers to multimodal / post-training / world model
#                  / generative / agent work.
#   noise       -- the plan's own noiseRules (career vent) or pure SEO copy.
#   placeholder -- the only URL is a stub, so no verifiable source exists.
#   job         -- it is a job posting that already lives in jobs.json, or a job
#                  posting whose URL is a stub; job facts belong in jobs.json.
#   truncated   -- the stored summary is a teaser cut mid-sentence; a deep read
#                  would have to invent the rest.
# --------------------------------------------------------------------------- #
SKIPS = [
    ("ec70af7a045b9f22", "off-domain",
     "标题与摘要均为阿尔茨海默病影像的可解释性（AXIAL），属医学影像诊断，方法层无可迁移到目标方向的内容。"),
    ("ad48db42eb955173", "off-domain",
     "摘要为 diffusion MRI 脑白质纤维束追踪（tractography）的 RNN/Transformer 评测，属神经影像学应用，非通用序列建模结论。"),
    ("2cb5ec505adcdc16", "off-domain",
     "摘要为英语语言教育中的 AI 趋势与预测，属教育学综述。"),
    ("e1a60ff5bcdd74ad", "off-domain",
     "摘要为面向领域研究的语言笔记本设计（教育/科研工具），与模型方法无关。"),
    ("e3c2e19e8960c634", "off-domain",
     "摘要为 AI 在法律领域的适用性、局限与治理，属法学/合规议题。"),
    ("a97194c18ca5df10", "off-domain",
     "摘要为算法正义与负责任 AI 新闻业的比较传播研究，属新闻传播学。"),
    ("2459123f398960a0", "off-domain",
     "摘要为 WiFi CSI 与 IMU 替代的可复现性研究，属无线感知。"),
    ("8398e354a2caa87f", "off-domain",
     "摘要为手法治疗+运动+心理干预的临床试验，属康复医学。"),
    ("56c25a19aad550c4", "off-domain",
     "摘要为可穿戴生物信号的特征生成（DeepFeature），目标域是健康监测，与目标方向的模型方法无交集。"),
    ("f321317c5c6a3438", "off-domain",
     "摘要为耳内加速度计的心跳身份识别，属生物特征识别硬件研究。"),
    ("c0494b509b63aae3", "off-domain",
     "摘要为无序蛋白区域的生成式设计，属蛋白质工程；训练目标虽借用了 RL/稀疏自编码器，但结论不可迁移。"),
    ("6cf8ef4124506a57", "off-domain",
     "摘要为切换拓扑传感器网络的广义 Kalman 一致性滤波，属分布式状态估计/控制理论。"),
    ("7a6ec1f53a0bfa09", "off-domain",
     "摘要为大数据在精神病学研究中的进展与约束，属精神病学/流行病学。"),
    ("8b47ba2a673f1961", "off-domain",
     "摘要为从社交媒体主题语料中迭代抽取结构化数据，属计算社会科学方法。"),
    ("49d205ddfa53b616", "off-domain",
     "摘要为异质介质中小吸收阱布置以最小化平均首达时间，属统计物理/扩散理论。"),
    ("f840200576ec6041", "off-domain",
     "摘要为驱动耗散光量子流体的集体激发综述，属量子光学。"),
    ("06aeb2509b595d9c", "off-domain",
     "摘要为非侵入式脑信号到文本的解码，属神经科学/BCI。"),
    ("65067c7852d59602", "off-domain",
     "摘要为分子量子动力学模拟的振子-量子比特原语，属量子化学/量子计算。"),
    ("87e3a2859a767275", "off-domain",
     "摘要为埃舍尔《画廊》版画与彭罗斯镶嵌的共形几何分析，属数学/艺术。"),
    ("1cffddbda5b078ac", "off-domain",
     "摘要为计算连续体（云-边-雾）数字孪生框架 Darpan，属分布式系统调度，与 RL 只共享关键词。"),
    ("b267ff96b62b124d", "off-domain",
     "摘要为跨平台数据库客户端 dbx（含 MCP Server 字样但主体是数据库 GUI），不是可深读的技术方法。"),
    ("df9779ef2a1128bd", "noise",
     "标题与正文是 Codex 海外手机号/GPT-5.6 API 替代的 SEO 导流文，无可核实的工程结论（trend 类）。"),
    ("4ee155d21249d863", "noise",
     "牛客求职吐槽帖（面试官滤镜/岗位跟风），命中 plan.policy.noiseRules 的 career-vent。"),
    ("38a5c854ac8c0e6f", "noise",
     "牛客情绪帖（招聘岗位收缩的体感），命中 plan.policy.noiseRules 的 career-vent。"),
    ("153213a1b791c02b", "truncated",
     "存储摘要是面经正文的截断片段（结尾为『为什_牛客网_牛客在手』），不足以支撑可证实的深度解析；待人工导入全文。"),
    ("b09e5bcd3bcd6d99", "placeholder",
     "URL 为占位符 https://jobs.bytedance.com/xxx，无真实 JD 链接；按 daily-agent.md 第 3 节不写入 jobs.json，转人工复核。"),
    ("07e0024fa4e3d622", "job",
     "腾讯『游戏AI-强化学习算法研究员』岗位帖，对应 jobs.json 中已有的 tencent-game-ai-rl-researcher，不重复深读。"),
    ("ad7ee6cb2ac86747", "job",
     "腾讯『WorkBuddy-Agent Harness 算法工程师』岗位帖，对应 jobs.json 中已有的 tencent-workbuddy-agent-harness，不重复深读。"),
]
SKIP_INDEX = {sid: (rule, reason) for sid, rule, reason in SKIPS}

# Fields copied from items/index.json into the per-batch input.  The sub-agent
# must not need anything else (that is what makes "no fabrication" checkable).
ITEM_FIELDS = ("id", "title", "category", "channel", "url", "sources", "tags",
               "summary", "authors", "publishedAt", "knowledgeType", "why",
               "relevanceScore", "qualitySignals", "peerReviewed", "codeAvailable")


def now_iso() -> str:
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


def chunk(seq, size):
    """Split into ceil(n/size) near-equal parts (no lonely 1-item tail)."""
    n = len(seq)
    if n == 0:
        return []
    k = -(-n // size)
    base, rem = divmod(n, k)
    out, start = [], 0
    for i in range(k):
        end = start + base + (1 if i < rem else 0)
        out.append(seq[start:end])
        start = end
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", default=_dt.date.today().isoformat())
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--work-dir", default=None,
                    help="default: scripts/agent-work-<date>")
    ap.add_argument("--queue-per-batch", type=int, default=5)
    ap.add_argument("--backfill-per-batch", type=int, default=3)
    args = ap.parse_args(argv)

    data_dir = args.data_dir if os.path.isabs(args.data_dir) else os.path.join(ROOT, args.data_dir)
    work_dir = args.work_dir or os.path.join(ROOT, "scripts", "agent-work-%s" % args.date)

    plan = read_json(os.path.join(data_dir, "deep-read-plan.json"))
    index = read_json(os.path.join(data_dir, "items", "index.json"))
    enrichment = read_json(os.path.join(data_dir, "enrichment.json"), {}) or {}
    if not plan or not index:
        raise SystemExit("deep-read-plan.json / items/index.json missing under %s" % data_dir)
    by_id = {i["id"]: i for i in index.get("items", []) if isinstance(i, dict)}
    enriched = enrichment.get("byId") or {}

    # ---- exclusions ------------------------------------------------------ #
    skips, unknown_skip_ids = [], []
    for sid, rule, reason in SKIPS:
        if sid not in by_id:
            unknown_skip_ids.append(sid)
    queue_ids = [q["id"] for q in plan.get("queue", [])]
    for sid, rule, reason in SKIPS:
        if sid in by_id:
            item = by_id[sid]
            skips.append({
                "id": sid, "rule": rule, "reason": reason,
                "inPlanQueue": sid in queue_ids,
                "title": item.get("title"), "category": item.get("category"),
                "channel": item.get("channel"), "url": item.get("url"),
                "stillSearchableInCardLibrary": True,
            })

    # ---- queue batches --------------------------------------------------- #
    kept = [q for q in plan.get("queue", []) if q["id"] not in SKIP_INDEX]
    batches, missing_ids = [], []
    for n, part in enumerate(chunk(kept, args.queue_per_batch), start=1):
        batch_id = "queue-%02d" % n
        items = []
        for q in part:
            src = by_id.get(q["id"])
            if src is None:
                missing_ids.append(q["id"])
                continue
            item = {k: src.get(k) for k in ITEM_FIELDS}
            item["planRelevanceScore"] = q.get("relevanceScore")
            item["planDepthScore"] = q.get("depthScore")
            items.append(item)
        payload = {
            "batchId": batch_id, "mode": "full", "date": args.date,
            "workDir": os.path.relpath(work_dir, ROOT).replace("\\", "/"),
            "instructions": "scripts/%s/INSTRUCTIONS.md" % os.path.basename(work_dir),
            "items": items,
        }
        write_json(os.path.join(work_dir, "in-%s.json" % batch_id), payload)
        batches.append({"batchId": batch_id, "mode": "full", "count": len(items),
                        "input": "in-%s.json" % batch_id,
                        "output": "out-%s.json" % batch_id,
                        "ids": [i["id"] for i in items]})

    # ---- backfill batches ------------------------------------------------ #
    for n, part in enumerate(chunk(plan.get("backfill", []), args.backfill_per_batch), start=1):
        batch_id = "backfill-%02d" % n
        items = []
        for b in part:
            src = by_id.get(b["id"])
            if src is None:
                missing_ids.append(b["id"])
                continue
            item = {k: src.get(k) for k in ITEM_FIELDS}
            item["missing"] = b.get("missing") or []
            item["backfillWeight"] = b.get("backfillWeight")
            item["existingEnrichment"] = enriched.get(b["id"])
            items.append(item)
        payload = {
            "batchId": batch_id, "mode": "backfill", "date": args.date,
            "workDir": os.path.relpath(work_dir, ROOT).replace("\\", "/"),
            "instructions": "scripts/%s/INSTRUCTIONS.md" % os.path.basename(work_dir),
            "items": items,
        }
        write_json(os.path.join(work_dir, "in-%s.json" % batch_id), payload)
        batches.append({"batchId": batch_id, "mode": "backfill", "count": len(items),
                        "input": "in-%s.json" % batch_id,
                        "output": "out-%s.json" % batch_id,
                        "ids": [i["id"] for i in items]})

    manifest = {
        "date": args.date,
        "generatedAt": now_iso(),
        "generatedBy": "scripts/plan_agent_batches.py",
        "workDir": os.path.relpath(work_dir, ROOT).replace("\\", "/"),
        "sourcePlan": {
            "file": "web/data/deep-read-plan.json",
            "generatedAt": plan.get("generatedAt"),
            "queueTotal": len(plan.get("queue", [])),
            "backfillTotal": len(plan.get("backfill", [])),
        },
        "excluded": len(skips),
        "queueToProcess": len(kept),
        "backfillToProcess": sum(len(b["ids"]) for b in batches if b["mode"] == "backfill"),
        "batches": batches,
        "missingItemIds": missing_ids,
        "unknownSkipIds": unknown_skip_ids,
    }
    write_json(os.path.join(work_dir, "manifest.json"), manifest)
    write_json(os.path.join(work_dir, "skips.json"), {
        "date": args.date,
        "generatedAt": manifest["generatedAt"],
        "generatedBy": "scripts/plan_agent_batches.py",
        "policy": "深读清单里可被证据证伪的条目按规则剔除；剔除不删除语料，条目仍留在卡片库可搜索、参与去重。",
        "rules": {
            "off-domain": "标题/摘要显示属于另一学科，方法层无可迁移内容",
            "noise": "命中 deep-read-plan.policy.noiseRules（求职吐槽/情绪帖）或纯 SEO 导流内容",
            "placeholder": "唯一 URL 为占位符，无真实来源可引用",
            "job": "岗位帖，事实归 jobs.json，不重复深读",
            "truncated": "存储摘要是截断片段，无法在不编造的前提下深读",
        },
        "skips": skips,
    })

    print("work dir        : %s" % work_dir)
    print("queue  total    : %d -> process %d, excluded %d" % (len(plan.get("queue", [])), len(kept), len(plan.get("queue", [])) - len(kept)))
    print("backfill total  : %d" % len(plan.get("backfill", [])))
    print("batches         : %d" % len(batches))
    for b in batches:
        print("  %-12s %-8s %d items" % (b["batchId"], b["mode"], b["count"]))
    if missing_ids:
        print("! ids in the plan but not in items/index.json: %s" % missing_ids)
    if unknown_skip_ids:
        print("! skip ids not found in items/index.json: %s" % unknown_skip_ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
