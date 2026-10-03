#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""build_problem_bank.py — 把采集到的**具体题目**抽成题库清单（题库定位的数据源）。

WHY THIS EXISTS
---------------
读者原话：「对于算法题和仓库的整理，直接给出仓库链接虽然可以保留，但我更希望你能根据题库
信息，阅读并自己深度解析相关手撕题、算法题等具体问题并整理到题库定位中附带深入的代码解析
或配图解析等」。

Before this script, 「题库定位」 came from exactly one place: the hand-curated
`research/jobs_kb.json` (22 手撕 + 25 笔试场景). The daily collector was already pulling
real problem statements (nowcoder 题霸 `/practice/`、牛客试题广场 `questionTerminal`、代码随想录
题解页) — 50+ concrete questions in the corpus — but they stayed as ordinary cards in the
card library, so nothing ever turned them into 题解, and the reader saw bare links.

This script is the deterministic half of the answer: it decides WHICH corpus items are
concrete, askable questions, and emits a stable work list. The non-deterministic half
(writing the 思路/复杂度/代码/配图) is the daily deep-read layer's job — it reads
`deep-read-plan.json`'s `problems` list, which is generated from this file.

DESIGN NOTES
------------
· Deterministic and offline: reads `items/index.json` only. No network, no LLM.
· Idempotent: pure function of the corpus + existing analyses. Re-running it rewrites the
  same file; nothing accumulates except through the corpus itself.
· `needsAnalysis` is derived from `problem-analysis.json`, so the queue shrinks
  permanently as the agent works through it — the same contract as `deep-read-plan.json`.
· Curated problems (jobs_kb) are listed under `curated` rather than merged into
  `problems`: they are already rendered by the frontend from jobs.json, and duplicating
  them would double every card. They still need analysis, so they need to be in the queue.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "web" / "data"
INDEX = DATA / "items" / "index.json"
JOBS = DATA / "jobs.json"
ANALYSIS = DATA / "problem-analysis.json"
OUT = DATA / "problem-bank.json"

CST = timezone(timedelta(hours=8))

SOURCE_NAMES = {
    "nowcoder_questions": "牛客题库",
    "nowcoder": "牛客讨论区",
    "programmercarl": "代码随想录",
    "kamacoder_notes": "卡码笔记",
    "github_algo": "GitHub 算法仓库",
    "inbox": "人工导入",
}

# 主题词典。Deliberately a flat, auditable lexicon rather than a classifier: the reader
# needs to see WHY a problem is filed under 动态规划, and a keyword hit is checkable.
TOPIC_RULES: list[tuple[str, "re.Pattern"]] = [
    ("链表", re.compile(r"链表|listnode|linked ?list", re.I)),
    ("二叉树", re.compile(r"二叉树|二叉搜索树|bst\b|树的中序|前序|后序|层序|遍历", re.I)),
    ("动态规划", re.compile(r"动态规划|dp\b|状态转移|背包|最长(?:公共|递增|回文)|编辑距离", re.I)),
    ("贪心", re.compile(r"贪心|greedy", re.I)),
    ("回溯", re.compile(r"回溯|dfs\b|全排列|子集|n 皇后|n皇后", re.I)),
    ("二分", re.compile(r"二分|binary search", re.I)),
    ("双指针", re.compile(r"双指针|two pointers|滑动窗口", re.I)),
    ("栈与队列", re.compile(r"栈|队列|stack|queue|单调栈", re.I)),
    ("哈希", re.compile(r"哈希|散列|hash|unordered_map", re.I)),
    ("堆与TopK", re.compile(r"\b堆\b|优先队列|topk|top k|第 ?k ?(?:大|小)", re.I)),
    ("图论", re.compile(r"图论|最短路|拓扑|最小生成树|并查集|dijkstra|floyd", re.I)),
    ("字符串", re.compile(r"字符串|回文|kmp|子序列|子串|正则", re.I)),
    ("数组与矩阵", re.compile(r"数组|矩阵|前缀和|差分|原地", re.I)),
    ("位运算", re.compile(r"位运算|位操作|异或|bit\b", re.I)),
    ("排序", re.compile(r"排序|快排|归并", re.I)),
    ("数学与概率", re.compile(r"概率|期望|贝叶斯|排列组合|质数|最大公约数|矩阵乘|单位向量|反射矩阵", re.I)),
    ("机器学习基础", re.compile(r"softmax|交叉熵|梯度|反向传播|归一化|正则化|损失函数|优化器|过拟合", re.I)),
    ("深度学习实现", re.compile(r"attention|transformer|卷积|池化|batchnorm|dropout|embedding|多头", re.I)),
    ("大模型与RL", re.compile(r"\bllm\b|大模型|强化学习|rlhf|dpo|grpo|ppo|推理|量化|蒸馏|rag|agent", re.I)),
    ("C++", re.compile(r"c\+\+|cpp|stl|虚函数|移动语义|智能指针|lambda", re.I)),
    ("Java", re.compile(r"\bjava\b|jvm|gc\b", re.I)),
    ("操作系统", re.compile(r"操作系统|进程|线程|死锁|内存管理|调度", re.I)),
    ("计算机网络", re.compile(r"网络|tcp|udp|http|握手", re.I)),
    ("数据库", re.compile(r"数据库|索引|事务|sql|mysql|redis", re.I)),
]

MARKER_RE = re.compile(r"^\s*\[(编程题|问答题|单选题|多选题)\]\s*")

# ---------------------------------------------------------------------------
# 实质度排序（为什么需要它）
# ---------------------------------------------------------------------------
# 牛客题霸 is published as a PROGRESSIVE list: it opens with warm-ups like 「判断字母」
# 「及格分数」「计算一元二次方程」. They pass the collector's relevance filter (the words
# 数组/字符串/输入 do appear), and they are real questions, so they belong in the bank - but
# if they occupy the daily 题解 quota the reader gets 20 trivial write-ups instead of
# 「反转链表」「编辑距离」「栈的压入弹出序列」. The quota is the scarce resource, so the
# ORDER has to express substance. Deterministically, two signals do that honestly:
#   · WHICH algorithm topics the statement mentions (动态规划/图/二叉树… are the interview
#     syllabus; 数学与概率/操作系统 are knowledge questions; 数组与矩阵 alone is thin);
#   · how much the statement actually says (a 60-character warm-up cannot carry a real 题解).
# A title that only asks for I/O formatting is penalised explicitly, because those titles
# repeat endlessly and no amount of statement length makes them interview material.
TOPIC_WEIGHT = {
    "动态规划": 3.0, "图论": 3.0, "二叉树": 3.0, "链表": 3.0, "回溯": 3.0,
    "二分": 2.5, "双指针": 2.5, "堆与TopK": 2.5, "哈希": 2.0, "字符串": 2.0,
    "栈与队列": 2.0, "排序": 2.0, "位运算": 2.0, "并查集": 2.5, "单调栈": 2.5,
    "数学与概率": 1.6, "机器学习基础": 2.2, "深度学习实现": 2.2, "大模型与RL": 2.2,
    "操作系统": 1.6, "计算机网络": 1.4, "数据库": 1.6, "C++": 1.6, "Java": 1.6,
    "数组与矩阵": 1.2, "贪心": 2.5,
}
TRIVIA_TITLE_RE = re.compile(
    r"判断(?:是|是不是)?(?:字母|元音|辅音|数字|奇偶)|及格分数|成绩(?:转换|等级)|"
    r"大小写转换|温度转换|简单计算|圆的面积|网购|竞选社长|单位阶跃|一元二次方程|"
    r"你是天才吗|变种水仙花|争夺前五名|位拆分|输出什么|打印(?:图形|菱形|九九)|"
    r"求和|平均值", re.I)

# ONE definition of "warm-up" for the whole pipeline: the collector refuses them at the
# source (collect.PROBLEM_WARMUP_RE) and the bank scores them down. Two copies would drift,
# and the drift would be invisible (one side keeps collecting what the other stopped
# scoring). The local copy is only a fallback so this script runs standalone.
WARMUP_RE = TRIVIA_TITLE_RE
try:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import collect as _warmup_src  # type: ignore
    WARMUP_RE = _warmup_src.PROBLEM_WARMUP_RE
except Exception:  # noqa: BLE001
    pass

# The "is this a concrete question?" rule lives in collect.py (one definition, used by the
# collector when it stamps `problemKind`). Import it instead of copying the regexes, with a
# local fallback so the bank builder never hard-fails if the collector's layout changes.
_PROBLEM_META = None
_EXCLUDE_RE = None
try:  # pragma: no cover - import cost is trivial, failure path is for safety
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import collect as _collect  # type: ignore
    _PROBLEM_META = _collect.problem_meta
    _EXCLUDE_RE = _collect.PROBLEM_EXCLUDE_RE
except Exception:  # noqa: BLE001
    _PROBLEM_META = None
    _EXCLUDE_RE = None

# 一道编程题必须**描述一个任务**。
#
# MEASURED MISS (found by an independent documentation pass, then reproduced twice):
#  · the first version accepted any of 判断|输入|输出|给定, and the programmercarl index page
#    「字符串学习路线」 — a study plan, not a question — contains 判断 in its summary. It
#    entered the bank with the HIGHEST substance score and took the first 题解 quota slot;
#  · the SECOND version over-corrected: requiring 时间限制|输入描述|给定一个是 far too strict
#    for 牛客 statements, because the collector trims the page to the statement body and the
#    metadata line ("时间限制：… 空间限制：…") is removed on purpose. It silently dropped real
#    problems such as 「栈的压入、弹出序列」.
# The lesson is that a single regex cannot carry this judgement, so the rule is now split:
# a STRONG title-level guard (a page ABOUT problems is not a problem) plus a WEAK statement
# signal (the text must at least look like a task) and a minimum length.
TITLE_NON_QUESTION_RE = re.compile(
    r"学习路线|路线图|roadmap|大纲|索引|目录|导航|汇总|合集|清单|总览|"
    r"模板|手册|指南|教程|入门|介绍|是什么|有哪些|区别|对比|"
    r"题单|刷题攻略|learning path|cheat ?sheet", re.I)

TASK_STATEMENT_RE = re.compile(
    r"输入|输出|给定|返回|实现|请你|请编写|请实现|编写|设计|计算|判断|排序|求|说明|"
    r"序列|数组|链表|二叉树|矩阵|字符串|栈|队列|哈希|图|函数|公式|概率|softmax|"
    r"attention|example\s*\d|input|output", re.I)

# 硬件/数字电路题：与算法岗面试无关（规则与 collect.py 的 PROBLEM_EXCLUDE_RE 同源，
# 这里再挡一次，因为题库还会被历史语料喂进来）。
HARDWARE_FALLBACK_RE = re.compile(
    r"译码器|编码器|触发器|时序电路|逻辑电路|门电路|寄存器|计数器|多路器|数据选择器|"
    r"全加器|半加器|全减器|奇偶校验|verilog|vhdl|fpga|卡诺图|布尔|与非门|或非门|"
    r"cmos|管脚|时钟树|复位信号|亚稳态", re.I)


def problem_kind(item: dict) -> str:
    """`problemKind` stamped by the collector, or derived with the same rule."""
    kind = item.get("problemKind")
    if kind in ("hand", "exam"):
        return kind
    if _PROBLEM_META is not None:
        try:
            return _PROBLEM_META(item)
        except Exception:  # noqa: BLE001
            return ""
    return ""


def now_iso() -> str:
    return datetime.now(CST).replace(microsecond=0).isoformat()


def load(path: Path, fallback):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return fallback


def topics_of(text: str) -> list[str]:
    return [name for name, pat in TOPIC_RULES if pat.search(text)][:6]


def substance_score(title: str, statement: str, topics: list[str]) -> float:
    """How much does this question deserve a 题解 quota slot? Higher is better, 0-10."""
    s = 0.0
    for t in topics:
        s += TOPIC_WEIGHT.get(t, 1.0)
    # Statement length: 200 chars is roughly where a statement starts describing a real
    # task (constraints + samples) rather than one sentence.
    s += min(3.0, len(statement) / 200.0)
    if WARMUP_RE.search(title):
        s -= 3.0
    # A knowledge question (单选题 about ML/probability) is worth less than a 手撕题 but not
    # nothing - it is still asked in 笔试.
    return round(max(0.0, min(10.0, s)), 2)


def estimate_difficulty(title: str, statement: str, topics: list[str]) -> str:
    """A coarse, explainable difficulty estimate (the sources rarely publish one)."""
    hard_markers = len(re.findall(
        r"动态规划|图论|最短路|拓扑|并查集|单调栈|字典树|kmp|状态压缩|线段树|"
        r"回溯|剪枝|数学|概率|期望|矩阵乘|attention", statement, re.I))
    if hard_markers >= 2 or len(statement) > 500:
        return "hard"
    if WARMUP_RE.search(title) or (not topics and len(statement) < 150):
        return "easy"
    if any(t in ("数组与矩阵",) for t in topics) and len(statement) < 260:
        return "easy"
    return "medium"


def clean_title(title: str) -> tuple[str, str]:
    """Return (marker, title-without-marker)."""
    m = MARKER_RE.match(title or "")
    if not m:
        return "", (title or "").strip()
    marker = m.group(1)
    rest = MARKER_RE.sub("", title).strip()
    # The 牛客 page title keeps a site suffix on older items ("…_牛客题霸_牛客网").
    rest = re.sub(r"_+牛客(?:题霸|网)?_*牛客网\s*$", "", rest).strip(" _-")
    rest = re.sub(r"\s*[|｜]\s*代码随想录.*$", "", rest).strip()
    return marker, rest


def bank_entry(item: dict, analyzed: set[str]) -> dict | None:
    kind = problem_kind(item)
    if kind not in ("hand", "exam"):
        return None
    pid = f"pb-{item.get('id')}"
    marker, title = clean_title(str(item.get("title") or ""))
    if not title:
        return None
    summary = str(item.get("summary") or "")
    hay = f"{title} {summary}"
    # 排除：数字电路/硬件题（与算法岗无关）；不是题目的索引/路线页。
    if (_EXCLUDE_RE or HARDWARE_FALLBACK_RE).search(hay):
        return None
    # A page ABOUT problems (学习路线/题单/汇总) is not a problem, however relevant its words.
    if TITLE_NON_QUESTION_RE.search(title):
        return None
    if kind == "hand" and not TASK_STATEMENT_RE.search(hay):
        return None
    if len(summary) < 60:
        return None
    topics = topics_of(hay)
    entry = {
        "id": pid,
        "itemId": item.get("id"),
        "kind": kind,
        "marker": marker or ("编程题" if kind == "hand" else "客观题"),
        "title": title,
        "statement": summary,
        "topics": topics,
        "difficulty": item.get("difficulty") or estimate_difficulty(title, summary, topics),
        "substance": substance_score(title, summary, topics),
        "sourceUrl": item.get("url"),
        "sourceName": SOURCE_NAMES.get(str(item.get("channel")), str(item.get("channel") or "")),
        "channel": item.get("channel"),
        "relevanceScore": item.get("relevanceScore"),
        "codeAvailable": bool(item.get("codeAvailable")),
        "needsAnalysis": pid not in analyzed,
    }
    return entry


def curated_entries(jobs: dict, analyzed: set[str]) -> list[dict]:
    """Hand-curated problems from jobs.json, each tagged with `needsAnalysis`.

    WHY analysed ones are kept instead of dropped: the bank is the id allow-list for the
    题解 validator and for build_agent_payload.py. If a curated id disappears the moment its
    题解 is written, the next validation reports a legitimate 题解 as "invented id" and the
    payload builder silently discards it - the exact class of bug that once threw away 15 of
    22 written 题解. The analysed ones therefore move to `curatedAnalyzed` rather than
    vanishing; `curated` stays the pending work list (contract: stats.curatedPending)."""
    out = []
    for src_key, kind in (("handWrittenCoding", "hand"), ("writtenExam", "exam")):
        for p in jobs.get(src_key) or []:
            if not isinstance(p, dict) or not p.get("id"):
                continue
            pid = str(p["id"])
            out.append({
                "id": pid,
                "kind": kind,
                "marker": "手撕代码" if kind == "hand" else "笔试场景",
                "title": str(p.get("title") or ""),
                "statement": str(p.get("prompt") or ""),
                "topics": [str(t) for t in (p.get("topics") or [])][:6] or topics_of(str(p.get("title") or "")),
                "difficulty": p.get("difficulty") or None,
                # Curated questions are human-vetted interview material, so they are trusted
                # rather than scored: the text heuristics exist to filter MACHINE-collected
                # questions. 手撕题 rank above 场景题 because a code-level 题解 is what the
                # reader asked for; the values sit above every collected problem's ceiling
                # except the very best 笔试客观题, so the daily quota fills with curated work
                # before it touches machine-collected trivia.
                "substance": 9.0 if kind == "hand" else 7.0,
                "sourceName": "人工整理（research/jobs_kb.json）",
                "origin": "curated",
                # What the curated entry already has, so the agent knows it only needs to
                # ADD the missing layers instead of rewriting what a human wrote.
                "has": {
                    "keyPoints": bool(p.get("keyPoints")),
                    "pitfalls": bool(p.get("pitfalls")),
                    "solution": bool(p.get("referenceSolution")),
                    "answerOutline": bool(p.get("answerOutline")),
                },
                "needsAnalysis": pid not in analyzed,
            })
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the 题库定位 problem bank")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    index = load(INDEX, None)
    if not index:
        print("problem-bank: items/index.json missing — run collect.py first", file=sys.stderr)
        return 1
    items = index.get("items") if isinstance(index, dict) else index
    items = items or []

    analysis = load(ANALYSIS, {}) or {}
    analyzed = set((analysis.get("byId") or {}).keys())

    problems, skipped = [], 0
    for item in items:
        e = bank_entry(item, analyzed)
        if e:
            problems.append(e)
        elif problem_kind(item):
            skipped += 1

    # Un-analysed first (that is the work list), then by how much the question is worth
    # (see substance_score): the quota is scarce, so 动态规划/图/链表 come before warm-ups.
    problems.sort(key=lambda e: (not e["needsAnalysis"],
                                 -(e.get("substance") or 0),
                                 -(e.get("relevanceScore") or 0)))

    jobs = load(JOBS, {}) or {}
    curated_all = curated_entries(jobs, analyzed)
    curated_all.sort(key=lambda e: str(e.get("id")))
    # `curated` is the pending work list; `curatedAnalyzed` keeps ids that already have a
    # 题解 so the validator/payload builder can still recognise them (see curated_entries).
    curated = [c for c in curated_all if c["needsAnalysis"]]
    curated_analyzed = [c for c in curated_all if not c["needsAnalysis"]]

    pending = sum(1 for p in problems if p["needsAnalysis"])
    payload = {
        "generatedAt": now_iso(),
        "source": "build_problem_bank.py",
        "corpusSize": len(items),
        "stats": {
            "total": len(problems),
            "hand": sum(1 for p in problems if p["kind"] == "hand"),
            "exam": sum(1 for p in problems if p["kind"] == "exam"),
            "analyzed": len(problems) - pending,
            "pending": pending,
            "curatedPending": len(curated),
            "curatedAnalyzed": len(curated_analyzed),
        },
        "problems": problems,
        "curated": curated,
        "curatedAnalyzed": curated_analyzed,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(out)

    if not args.quiet:
        s = payload["stats"]
        print(f"problem bank: {s['total']} 题（手撕 {s['hand']} / 场景 {s['exam']}）"
              f" · 已解析 {s['analyzed']} · 待解析 {s['pending']}"
              f" · 人工题待补 {s['curatedPending']} · 人工题已解析 {s['curatedAnalyzed']}")
        if skipped:
            print(f"  ({skipped} 条 problemKind 条目标题异常被跳过)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
