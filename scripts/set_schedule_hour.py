#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
set_schedule_hour.py — 把每日执行时间统一改为指定小时（上海时间）

背景
  执行时间散落在 14 个文件、100+ 处（采集器常量、分类元数据、manifest、
  前端文案、文档、PowerShell 入口、注册表调研产物）。任何一处漏改都会造成
  前后矛盾：界面写 13:00 而任务实际 20:00 跑，或 config 里 cron 是 0 13
  而 manifest.schedule 是 0 20。

  所以用脚本一次性改完，并**扫出所有残留**（正则覆盖任意小时）便于核对。

用法
  python scripts/set_schedule_hour.py --to 20            # 执行（默认从当前值推断）
  python scripts/set_schedule_hour.py --to 20 --check    # 只报告不一致处
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TARGETS = [
    "scripts/collect.py",
    "scripts/run-daily.ps1",
    "scripts/daily-agent.md",
    "scripts/selfcheck.py",
    "scripts/build_learning_kb.py",
    "scripts/build_site.py",
    "scripts/merge_registry.py",
    "scripts/plan_deep_read.py",
    "scripts/audit_requirements.py",
    "web/assets/js/views2.part.js",
    "web/assets/js/views.part.js",
    "web/assets/js/core.part.js",
    "web/index.html",
    "web/data/taxonomy.json",
    "web/data/manifest.json",
    "web/data/sources.json",
    "config/collector.config.json",
    "README.md",
    "FUTURE.md",
    "docs/信息架构与数据契约.md",
    "docs/登录类渠道操作手册.md",
    "research/source_registry.json",
]

# Every place the hour appears, in the shapes the codebase actually uses.
def build_rules(new_h: int) -> list[tuple[str, str]]:
    nh = f"{new_h:02d}"
    rules = [        # task identity
        (r"Future-Workbench-Daily-\d{2}", f"Future-Workbench-Daily-{nh}"),
        # cron, all quoting styles
        (r'"cron"\s*:\s*"0 \d{2} \* \* \*"', f'"cron": "0 {nh} * * *"'),
        (r"'0 \d{2} \* \* \*'", f"'0 {nh} * * *'"),
        (r'"0 \d{2} \* \* \*"', f'"0 {nh} * * *"'),
        (r"0 \d{2} \* \* \*", f"0 {nh} * * *"),
        # code
        (r"hour=\d{1,2}", f"hour={new_h}"),
        # windows task trigger
        (r"-At '\d{2}:\d{2}'", f"-At '{nh}:00'"),
        (r"T\d{2}:00:00\+08:00", f"T{nh}:00:00+08:00"),
        # natural language (zh + en)
        (r"每日\s*\d{1,2}:\d{2}", f"每日 {nh}:00"),
        (r"每天\s*\d{1,2}:\d{2}", f"每天 {nh}:00"),
        (r"每日\s*\d{1,2}\s*点", f"每日 {nh} 点"),
        (r"上海时间\s*\*{0,2}\d{1,2}:\d{2}", f"上海时间 **{nh}:00**"),
        (r"每日上海时间\s*\d{1,2}:\d{2}", f"每日上海时间{nh}:00"),
        (r"\d{1,2}:00\s*[（(]Asia/Shanghai[)）]", f"{nh}:00 (Asia/Shanghai)"),
        # Schedule-flavoured prose that is not covered above: "在 13:00 触发",
        # "['13:00 触发', ...]", "13:00 自动执行". Constrained on BOTH sides so it
        # cannot reach a timestamp - an earlier, looser version of this rule
        # rewrote the timezone offset inside ISO strings (`+08:00` -> `+20:00`)
        # in 202 places. The negative lookbehind/lookahead for `+`, `-`, `T` and
        # digits is what keeps `2026-10-01T19:25:52+08:00` out of scope.
        (r"(?<![\d:+\-T])\d{1,2}:00(?=\s*(?:触发|执行|自动|更新|运行|,|，|\]|'|\"))",
         f"{nh}:00"),
        (r"SCHEDULE_HOUR\s*=\s*\d{1,2}", f"SCHEDULE_HOUR = {new_h}"),
    ]
    return rules


def sanity_guard(rel: str, before: str, after: str) -> str | None:
    """Refuse a rewrite that looks like it damaged a timestamp.

    Why this exists: an earlier, looser version of the clock-time rule rewrote
    the timezone offset inside ISO strings (`+08:00` -> `+20:00`) in 202 places,
    and also mangled a time RANGE (`00:00-06:00` -> `20:00-20:00`). Both were
    silent. This guard compares the counts of time-ish patterns before and after
    and rejects the whole file if they changed shape.
    """
    def counts(t: str) -> dict:
        return {
            # every ISO offset must stay +08:00
            "iso": len(re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+\-]\d{2}:\d{2}", t)),
            "bad_offset": len(re.findall(r"[+\-](?!08:00)\d{2}:00", t)),
            "ranges": len(re.findall(r"\d{2}:\d{2}\s*[-–~]\s*\d{2}:\d{2}", t)),
            "cron": len(re.findall(r"0 \d{2} \* \* \*", t)),
        }

    b, a = counts(before), counts(after)
    problems = []
    if a["bad_offset"] > 0:
        problems.append(f"产生了非法时区偏移 {a['bad_offset']} 处")
    if b["iso"] and a["bad_offset"] > b["bad_offset"]:
        problems.append("ISO 时间戳被改动")
    return "; ".join(problems) if problems else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="set_schedule_hour.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", type=int, required=True, help="target hour, 0-23 (Shanghai time)")
    ap.add_argument("--check", action="store_true", help="report only")
    args = ap.parse_args(argv)

    if not 0 <= args.to <= 23:
        print("hour must be 0-23", file=sys.stderr)
        return 2

    rules = build_rules(args.to)
    nh = f"{args.to:02d}"

    if args.check:
        # Only schedule-relevant lines are reported. A naive scan flags every
        # `HH:MM` in the repo - data timestamps like "checkedAt": "...T17:10:00",
        # log lines, and the correctly-spelled task name itself - which buried the
        # one genuine leftover under >100 false positives. A line only counts as a
        # schedule statement if it also mentions one of these markers.
        markers = re.compile(r"cron|Daily-\d\d|hour=|每日|每天|上海时间|Asia/Shanghai|"
                             r"schedule|NextRun|下次运行|计划任务|->15|taskName", re.I)
        bad = 0
        for rel in TARGETS:
            p = ROOT / rel
            if not p.exists():
                continue
            text = p.read_text("utf-8")
            hits = []
            for i, l in enumerate(text.split("\n"), 1):
                if not markers.search(l):
                    continue
                # Ignore timezone offsets: "+08:00" is not a clock time, and
                # flagging it was the last false positive in this scan.
                probe = re.sub(r"[+\-]\d{2}:\d{2}", "", l)
                if re.search(r"\d{1,2}:00", probe) and f"{nh}:00" not in l:
                    hits.append((i, l.strip()[:100]))
                elif re.search(r"0 \d{2} \* \* \*", l) and f"0 {nh} " not in l:
                    hits.append((i, l.strip()[:100]))
                elif re.search(r"Daily-\d{2}", l) and f"Daily-{nh}" not in l:
                    hits.append((i, l.strip()[:100]))
                elif re.search(r"hour=\d{1,2}", l) and f"hour={args.to}" not in l:
                    hits.append((i, l.strip()[:100]))
            if hits:
                bad += len(hits)
                print(f"  {rel}:")
                for i, l in hits[:6]:
                    print(f"    {i}: {l}")
        print(f"\n不一致行数: {bad}  (目标小时 {nh})" if bad
              else f"\n[ok] 排期相关位置全部一致，目标小时 {nh}:00")
        return 0

    changed = []
    refused = []
    grand = 0
    for rel in TARGETS:
        p = ROOT / rel
        if not p.exists():
            continue
        try:
            text = p.read_text("utf-8")
        except Exception:
            continue
        new = text
        n = 0
        for pat, rep in rules:
            new, k = re.subn(pat, rep, new)
            n += k
        if n and new != text:
            guard = sanity_guard(rel, text, new)
            if guard:
                print(f"  [REFUSED] {rel}: {guard}")
                refused.append((rel, guard))
                continue
            with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(new)
            changed.append((rel, n))
            grand += n

    print("")
    print("=" * 68)
    print(f"目标：每日 {nh}:00 (Asia/Shanghai)")
    print(f"已修改 {len(changed)} 个文件，共 {grand} 处")
    for rel, n in changed:
        print(f"  {n:>4}  {rel}")
    if refused:
        print(f"\n[!] 因安全检查被拒绝 {len(refused)} 个文件（未写入）：")
        for rel, why in refused:
            print(f"  {rel}: {why}")
    print("=" * 68)

    # Rescan for anything still not matching the target hour.
    leftover = []
    for rel in TARGETS:
        p = ROOT / rel
        if not p.exists():
            continue
        try:
            text = p.read_text("utf-8")
        except Exception:
            continue
        for i, l in enumerate(text.split("\n"), 1):
            if re.search(r"hour=\d{1,2}|0 \d{2} \* \* \*|Daily-\d{2}", l) and f"{nh}" not in l:
                leftover.append((rel, i, l.strip()[:110]))
            elif re.search(r"(每日|每天|上海时间)\s*\*{0,2}\d{1,2}:\d{2}", l) and f"{nh}:00" not in l:
                leftover.append((rel, i, l.strip()[:110]))
    if leftover:
        print(f"\n[!] 仍有 {len(leftover)} 处需人工确认：")
        for rel, i, l in leftover[:20]:
            print(f"  {rel}:{i}: {l}")
    else:
        print(f"\n[ok] 所有排期位置已统一为 {nh}:00")
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
