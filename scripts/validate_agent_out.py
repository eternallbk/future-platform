#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hard-validate the deep-read output of the daily agent (layer 2).

Checks every ``out-<batch>.json`` against its ``in-<batch>.json``:

* ids -- one entry per input item, ids identical, no duplicates, no extras;
* shape -- ``full`` needs all 12 top-level fields, ``backfill`` may only write
  the layers listed in ``missing``;
* ``diagram`` -- kind/title/caption/alt present, and a合规 inline SVG or a
  structured ``spec``; the SVG must be self-contained (no ``<image>``,
  ``<use>``, ``<style>``, ``<script>``, ``url(...)``, ``on*=``) and legible;
* ``teaching`` -- the ten layers, ``progressive`` with four levels;
* formulas -- five layers, canonical ``category``, and a URL that the corpus
  really carries (rule 2 of daily-agent.md);
* no empty-string filler in required prose.

Usage
-----
    python scripts/validate_agent_out.py --date 2026-10-03
    python scripts/validate_agent_out.py --date 2026-10-03 --strict   # warnings fail too
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_DIR = os.path.join(ROOT, "web", "data")

CANONICAL_CATEGORIES = {"multimodal", "posttraining", "worldmodel", "generative",
                        "rl", "agent", "foundation", "engineering"}
DIAGRAM_KINDS = {"flow", "architecture", "curve", "matrix", "timeline"}
DIFFICULTIES = {"easy", "medium", "hard"}

FULL_REQUIRED = ("id", "tldr", "keyPoints", "why", "difficulty", "tags", "entities",
                 "diagram", "concepts", "formulas", "selfCheck", "teaching")
TEACHING_REQUIRED = ("progressive", "socratic", "mechanism", "boundary",
                     "analogyBoundary", "misconceptions", "ownAnalysis",
                     "officialNotes", "interviewAnswer", "studyPath")
CONCEPT_REQUIRED = ("name", "readable", "analogy", "analogyBreaksDown", "mechanism",
                    "visual", "prerequisites", "selfTest", "formula")
FORMULA_LAYERS = ("symbols", "derivation", "analogy", "code", "pitfalls")
BACKFILL_LAYERS = {"teaching", "diagram", "formulas", "concepts"}

URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
# XML/SVG namespace declarations are not "sources" -- they are part of the markup.
URL_ALLOWLIST = ("http://www.w3.org/2000/svg", "https://www.w3.org/2000/svg",
                 "http://www.w3.org/1999/xlink")
# Trailing CJK/full-width punctuation that a regex easily swallows.
URL_TRAILING = "）)】》。，、；：！？\"'”’>,.;:"

# (label, predicate, limit) -- over-length is a warning, not an error.
LENGTH_RULES = (
    ("tldr", 60), ("why", 80),
)


def now_iso():
    return _dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def urls_of(item):
    """Every URL the corpus itself carries for this item: url / sources[] and
    any link quoted inside its own title/summary text."""
    out = set()
    if item.get("url"):
        out.add(item["url"].strip())
    for s in item.get("sources") or []:
        if isinstance(s, dict) and s.get("url"):
            out.add(s["url"].strip())
    text = "%s\n%s" % (item.get("title") or "", item.get("summary") or "")
    for raw in URL_RE.findall(text):
        out.add(clean_url(raw))
    return out


def norm_url(u):
    return (u or "").strip().rstrip("/")


def clean_url(u):
    """Strip trailing CJK punctuation the URL regex may have swallowed."""
    u = (u or "").strip()
    while u and u[-1] in URL_TRAILING:
        u = u[:-1]
    return u


def is_allowlisted_url(u):
    return any(u.startswith(p) for p in URL_ALLOWLIST)


class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def err(self, where, msg):
        self.errors.append("%s: %s" % (where, msg))

    def warn(self, where, msg):
        self.warnings.append("%s: %s" % (where, msg))


def check_svg(where, svg, rep):
    if not isinstance(svg, str) or "<svg" not in svg:
        rep.err(where, "svg 不是内联 <svg>")
        return
    if "viewBox" not in svg:
        rep.err(where, "svg 缺少 viewBox")
    for bad in ("<image", "<use", "<style", "<script", "url(", "onload", "onclick"):
        if bad in svg:
            rep.err(where, "svg 含禁止内容 %r" % bad)
    for m in re.finditer(r"font-size\s*=\s*['\"]?(\d+)", svg):
        if int(m.group(1)) < 13:
            rep.err(where, "svg font-size %s < 13" % m.group(1))
    if "xmlns" not in svg:
        rep.warn(where, "svg 缺少 xmlns")


def check_diagram(where, d, rep):
    if not isinstance(d, dict):
        rep.err(where, "diagram 不是对象")
        return
    for k in ("kind", "title", "caption", "alt"):
        if not (d.get(k) or "").strip():
            rep.err(where, "diagram.%s 缺失/为空" % k)
    if d.get("kind") not in DIAGRAM_KINDS:
        rep.err(where, "diagram.kind=%r 不在 %s" % (d.get("kind"), sorted(DIAGRAM_KINDS)))
    title = d.get("title") or ""
    if len(title) > 20:
        rep.warn(where, "diagram.title 超过 20 字（%d）" % len(title))
    has_svg = bool((d.get("svg") or "").strip())
    spec = d.get("spec")
    has_spec = isinstance(spec, dict) and bool(spec.get("type"))
    if not has_svg and not has_spec:
        rep.err(where, "diagram 既没有 svg 也没有 spec")
    if has_svg:
        check_svg(where, d["svg"], rep)
    if has_spec and spec.get("type") not in DIAGRAM_KINDS:
        rep.err(where, "diagram.spec.type=%r 非法" % spec.get("type"))


def check_teaching(where, t, rep):
    if not isinstance(t, dict):
        rep.err(where, "teaching 不是对象")
        return
    missing = [k for k in TEACHING_REQUIRED if k not in t]
    if missing:
        rep.err(where, "teaching 缺字段 %s" % missing)
    prog = t.get("progressive")
    if not isinstance(prog, list) or len(prog) != 4:
        rep.err(where, "teaching.progressive 必须是 4 档（实际 %s）"
                % (len(prog) if isinstance(prog, list) else type(prog).__name__))
        prog = prog if isinstance(prog, list) else []
    for i, p in enumerate(prog):
        if not isinstance(p, dict) or not (p.get("level") or "").strip() or not (p.get("text") or "").strip():
            rep.err(where, "teaching.progressive[%d] 缺 level/text" % i)
    soc = t.get("socratic")
    if not isinstance(soc, list) or len(soc) < 2:
        rep.err(where, "teaching.socratic 至少 2 组（实际 %s）" % (len(soc) if isinstance(soc, list) else "?"))
    else:
        for i, s in enumerate(soc):
            if not isinstance(s, dict) or not (s.get("q") or "").strip() or not (s.get("a") or "").strip():
                rep.err(where, "teaching.socratic[%d] 缺 q/a" % i)
    for k in ("mechanism", "boundary", "analogyBoundary", "ownAnalysis", "interviewAnswer"):
        if not (t.get(k) or "").strip():
            rep.err(where, "teaching.%s 为空" % k)
    misc = t.get("misconceptions")
    if not isinstance(misc, list) or len(misc) < 2:
        rep.err(where, "teaching.misconceptions 至少 2 条（实际 %s）" % (len(misc) if isinstance(misc, list) else "?"))
    on = t.get("officialNotes")
    if not isinstance(on, list):
        rep.err(where, "teaching.officialNotes 必须是数组（可为空）")
    else:
        for i, o in enumerate(on):
            if not isinstance(o, dict) or not (o.get("position") or "").strip() or not (o.get("source") or "").strip():
                rep.err(where, "teaching.officialNotes[%d] 缺 position/source" % i)
    sp = t.get("studyPath")
    if not isinstance(sp, dict) or not sp.get("prerequisites") or not sp.get("nextSteps"):
        rep.err(where, "teaching.studyPath 需要非空 prerequisites 与 nextSteps")


def check_concepts(where, concepts, rep):
    if not isinstance(concepts, list) or not concepts:
        rep.err(where, "concepts 必须是非空数组")
        return
    if len(concepts) > 3:
        rep.warn(where, "concepts 超过 3 个（%d）" % len(concepts))
    for i, c in enumerate(concepts):
        if not isinstance(c, dict):
            rep.err(where, "concepts[%d] 不是对象" % i)
            continue
        for k in CONCEPT_REQUIRED:
            if k not in c:
                rep.err(where, "concepts[%d] 缺字段 %s" % (i, k))
        for k in CONCEPT_REQUIRED:
            if k in ("formula", "prerequisites", "selfTest"):
                continue
            if not (c.get(k) or "").strip():
                rep.err(where, "concepts[%d].%s 为空" % (i, k))
        if not isinstance(c.get("prerequisites"), list) or not c.get("prerequisites"):
            rep.err(where, "concepts[%d].prerequisites 需非空数组" % i)
        if not isinstance(c.get("selfTest"), list) or len(c.get("selfTest") or []) < 3:
            rep.err(where, "concepts[%d].selfTest 需要 3 条" % i)


def check_formulas(where, formulas, allowed, rep):
    if not isinstance(formulas, list):
        rep.err(where, "formulas 不是数组")
        return
    for i, f in enumerate(formulas):
        w = "%s formulas[%d]" % (where, i)
        if not isinstance(f, dict):
            rep.err(w, "不是对象")
            continue
        fid = f.get("id") or ""
        if not re.fullmatch(r"f-[a-z0-9][a-z0-9-]*", fid):
            rep.err(w, "id 非法：%r" % fid)
        if not (f.get("name") or "").strip():
            rep.err(w, "name 为空")
        if f.get("category") not in CANONICAL_CATEGORIES:
            rep.err(w, "category=%r 非法" % f.get("category"))
        if not (f.get("latex") or "").strip():
            rep.err(w, "latex 为空")
        for k in FORMULA_LAYERS:
            if not f.get(k):
                rep.err(w, "缺少层 %s" % k)
        sym = f.get("symbols")
        if not isinstance(sym, list) or not sym:
            rep.err(w, "symbols 需非空数组")
        else:
            for j, s in enumerate(sym):
                if not isinstance(s, list) or len(s) != 2 or not all(isinstance(x, str) and x.strip() for x in s):
                    rep.err(w, "symbols[%d] 必须是 [符号, 含义]" % j)
        der = f.get("derivation")
        if not isinstance(der, list) or not (2 <= len(der) <= 5):
            rep.err(w, "derivation 需要 2-5 步（实际 %s）" % (len(der) if isinstance(der, list) else "?"))
        src = f.get("sources")
        if not isinstance(src, list) or not src:
            rep.err(w, "sources 需非空数组")
        else:
            for j, s in enumerate(src):
                if not isinstance(s, dict) or not (s.get("url") or "").strip():
                    rep.err(w, "sources[%d] 缺 url" % j)
                    continue
                if allowed and norm_url(clean_url(s["url"])) not in allowed:
                    rep.err(w, "sources[%d].url 不在该条目已有 URL 里：%s" % (j, s["url"]))


def check_selfcheck(where, sc, rep):
    if not isinstance(sc, dict):
        rep.err(where, "selfCheck 不是对象")
        return
    if not isinstance(sc.get("claims"), list) or not sc.get("claims"):
        rep.err(where, "selfCheck.claims 需非空数组")
    if not isinstance(sc.get("uncertain"), list):
        rep.err(where, "selfCheck.uncertain 必须是数组")


def check_provenance(where, entry, allowed, rep):
    """Every URL in the entry must come from the item itself (rule 2)."""
    blob = json.dumps(entry, ensure_ascii=False)
    for raw in URL_RE.findall(blob):
        u = clean_url(raw)
        if is_allowlisted_url(u):
            continue
        if norm_url(u) not in allowed:
            rep.err(where, "条目里出现数据中没有的 URL：%s" % u)


def validate_batch(work_dir, batch, rep):
    in_path = os.path.join(work_dir, batch["input"])
    out_path = os.path.join(work_dir, batch["output"])
    batch_id = batch["batchId"]
    src = read_json(in_path)
    if src is None:
        rep.err(batch_id, "缺少输入文件 %s" % batch["input"])
        return 0
    if not os.path.exists(out_path):
        rep.err(batch_id, "缺少输出文件 %s" % batch["output"])
        return 0
    try:
        out = read_json(out_path)
    except Exception as exc:  # noqa: BLE001
        rep.err(batch_id, "输出不是合法 JSON：%s" % exc)
        return 0
    if not isinstance(out, dict) or not isinstance(out.get("entries"), list):
        rep.err(batch_id, "输出必须是 {batchId, entries:[...]}")
        return 0
    if out.get("batchId") not in (None, batch_id):
        rep.err(batch_id, "输出 batchId=%r 与输入不一致" % out.get("batchId"))

    items = {i["id"]: i for i in src.get("items", [])}
    entries = out["entries"]
    seen = {}
    for e in entries:
        if not isinstance(e, dict):
            rep.err(batch_id, "entry 不是对象")
            continue
        eid = e.get("id")
        if eid not in items:
            rep.err(batch_id, "出现输入里没有的 id：%r" % eid)
            continue
        if eid in seen:
            rep.err(batch_id, "id 重复：%s" % eid)
            continue
        seen[eid] = e
    for eid in items:
        if eid not in seen:
            rep.err(batch_id, "缺少 id 的 entry：%s" % eid)
    if [e.get("id") for e in entries] != [e.get("id") for e in entries if isinstance(e, dict)]:
        pass

    n_ok = 0
    for eid, entry in seen.items():
        item = items[eid]
        where = "%s/%s" % (batch_id, eid)
        allowed = {norm_url(u) for u in urls_of(item)}
        mode = src.get("mode")
        if mode == "backfill":
            missing = set(item.get("missing") or [])
            extra = [k for k in entry if k not in missing and k != "id"]
            if extra:
                rep.err(where, "backfill 只应补 %s，却多写了 %s" % (sorted(missing), sorted(extra)))
            if "teaching" in missing and "teaching" in entry:
                check_teaching(where, entry["teaching"], rep)
            if "diagram" in missing and "diagram" in entry:
                check_diagram(where, entry["diagram"], rep)
            if "formulas" in missing and "formulas" in entry:
                check_formulas(where, entry["formulas"], allowed, rep)
            if "concepts" in missing and "concepts" in entry:
                check_concepts(where, entry["concepts"], rep)
        else:
            missing = [k for k in FULL_REQUIRED if k not in entry]
            if missing:
                rep.err(where, "full 模式缺顶层字段 %s" % missing)
            if not (entry.get("tldr") or "").strip():
                rep.err(where, "tldr 为空")
            kp = entry.get("keyPoints")
            if not isinstance(kp, list) or not (3 <= len(kp) <= 5):
                rep.err(where, "keyPoints 需要 3-5 条（实际 %s）" % (len(kp) if isinstance(kp, list) else "?"))
            if not (entry.get("why") or "").strip():
                rep.err(where, "why 为空")
            if entry.get("difficulty") not in DIFFICULTIES:
                rep.err(where, "difficulty=%r 非法" % entry.get("difficulty"))
            tags = entry.get("tags")
            if not isinstance(tags, list) or not (3 <= len(tags) <= 6):
                rep.err(where, "tags 需要 3-6 个（实际 %s）" % (len(tags) if isinstance(tags, list) else "?"))
            elif any(t != t.lower() for t in tags):
                rep.warn(where, "tags 里有大写：%s" % tags)
            if not isinstance(entry.get("entities"), list):
                rep.err(where, "entities 不是数组")
            check_diagram(where, entry.get("diagram"), rep)
            check_concepts(where, entry.get("concepts"), rep)
            check_formulas(where, entry.get("formulas"), allowed, rep)
            check_selfcheck(where, entry.get("selfCheck"), rep)
            check_teaching(where, entry.get("teaching"), rep)
            for field, limit in LENGTH_RULES:
                txt = entry.get(field) or ""
                if len(txt) > limit:
                    rep.warn(where, "%s 超过 %d 字（%d）" % (field, limit, len(txt)))
        check_provenance(where, entry, allowed, rep)
        n_ok += 1
    return n_ok


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", default=_dt.date.today().isoformat())
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--strict", action="store_true", help="warnings also fail")
    args = ap.parse_args(argv)

    work_dir = args.work_dir or os.path.join(ROOT, "scripts", "agent-work-%s" % args.date)
    manifest = read_json(os.path.join(work_dir, "manifest.json"))
    if not manifest:
        raise SystemExit("manifest not found under %s (run plan_agent_batches.py first)" % work_dir)

    rep = Report()
    total = 0
    for batch in manifest.get("batches", []):
        total += validate_batch(work_dir, batch, rep)

    print("work dir : %s" % work_dir)
    print("batches  : %d" % len(manifest.get("batches", [])))
    print("entries  : %d" % total)
    for w in rep.warnings:
        print("WARN  %s" % w)
    for e in rep.errors:
        print("ERROR %s" % e)
    print("errors=%d warnings=%d" % (len(rep.errors), len(rep.warnings)))
    if rep.errors or (args.strict and rep.warnings):
        print("VALIDATION FAILED")
        return 1
    print("VALIDATION OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
