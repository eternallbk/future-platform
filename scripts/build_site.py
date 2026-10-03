#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_site.py — 生成**可公开发布**的静态站点（dist/）

背景
  你要在非本地和手机上阅读工作台，所以需要一个能放到静态托管的版本。
  但工作台目录里有两类东西**绝对不能公开**：

  1. `web/data/state/collector-state.json`（约 1 MB）——
     里面是全部去重指纹、firstSeen/lastSeen、以及**原始 canonical 条目**
     （含尚未进入索引的字段）。公开它既没必要，也等于泄露采集内部状态。
  2. `web/data/inbox/` —— 你手工导入的内容（小红书/BOSS直聘导出），
     可能含内推码、备注等个人信息。
  3. `web/data/logs/agent-*.json` 与 `proposals/` 里的本地路径、
     以及 `enrichment.json` 里 `selfCheck.uncertain` 的待核实内容。

  做法：**白名单**输出（只复制明确要公开的文件），不是黑名单排除。
  加一个新数据文件时如果忘了加到白名单，它会**不出现在站点上**——
  宁可漏发也不要误发。

用法:
  python scripts/build_site.py                     # 输出到 dist/
  python scripts/build_site.py --out D:\\site      # 指定输出目录
  python scripts/build_site.py --check             # 只审计与统计，不写
  python scripts/build_site.py --keep-state        # 连 state 一起发（不推荐）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DATA = WEB / "data"
CST = timezone(timedelta(hours=8))

# Directories copied verbatim from web/ (front-end assets only).
COPY_DIRS = ["assets"]

# Directories copied from web/data/ (public data only).
DATA_DIRS = ["digest", "items", "logs", "proposals"]

# Individual data files safe to publish.
# NOTE: `items.json` is deliberately absent - it is a byte-identical copy of
# `items/index.json` that only the local path uses, so shipping both doubles
# ~1.1 MB of payload for nothing.
DATA_FILES = [
    "manifest.json",
    "taxonomy.json",
    "sources.json",
    "jobs.json",
    "learning.json",
    "formulas.json",
    "enrichment.json",
    "deep-read-plan.json",
    # The redundancy audit (analyze_redundancy.py, run every day). Published so the
    # 采集与运行 view can show the worklist next to channel health - redundancy is a
    # maintenance signal the reader should be able to see without opening the repo.
    # It contains only counts, token frequency and item titles, no private state.
    "redundancy-report.json",
    # Interview extract (build_interview_index.py): company / round / outcome /
    # topics per 面经. Public - it only restates item titles and links.
    "interview.json",
    # Corpus audit (audit_corpus.py): per-item verdicts, reasons and the removal
    # worklist. Published so the maintenance view can show WHY something is a removal
    # candidate rather than only reporting how many items exist. Titles and reasons
    # only, no private state.
    "corpus-audit.json",
    # 题库定位：problem-bank.json（采集到的具体题目，确定性生成）与
    # problem-analysis.json（每日深读写出的题解：思路/复杂度/代码/配图）。
    # 两者都只包含公开题目与自写解析，不含任何本地状态；不发布的话「题库定位」
    # 在线上只有人工整理的 47 题，算法题库整块消失。
    "problem-bank.json",
    "problem-analysis.json",
]

# Front-end SOURCE files: the parts are concatenated into app.js at build time,
# so shipping them would duplicate ~250 KB of code and expose the authoring
# layout. Only the generated bundle belongs on the published site.
DENY_ASSET_SUFFIX = (".part.js", ".map", ".tmp")

# Anything matching these is NEVER published, even if it appears in a copied dir.
DENY_NAMES = {"collector-state.json"}
DENY_DIRS = {"state", "inbox", "archive", "__pycache__"}

# log/artifact filenames that leak local paths or unfinished analysis
DENY_SUFFIX = (".tmp",)
DENY_LOG_PREFIX = ("harness-",)      # local run logs: keep the machine-local ones out


def human(n: int) -> str:
    for unit in ("B", "KiB", "MiB"):
        if n < 1024 or unit == "MiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n} B"


def read_json(path: Path):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return None


def prune_logs(dst_dir: Path) -> list[str]:
    """Remove machine-local logs from the copied logs directory."""
    removed = []
    if not dst_dir.exists():
        return removed
    for p in sorted(dst_dir.iterdir()):
        if p.is_file() and (p.name.startswith(DENY_LOG_PREFIX) or p.name.endswith(DENY_SUFFIX)):
            p.unlink()
            removed.append(p.name)
    return removed


def clear_output(out: Path) -> None:
    """Empty the output directory, clearing read-only attributes first.

    The output is removed CONTENT-BY-CONTENT rather than by deleting the directory,
    because Windows refuses to unlink read-only files - observed as
    `PermissionError: [WinError 5]`, which aborted the entire build and therefore
    silently skipped the publish step that follows it.

    History: an earlier publish script kept a nested git repository inside dist/,
    and this function had to preserve `.git`. That design was abandoned - it let
    git's directory discovery escape into the project repository and created a
    bogus gh-pages branch there - so the deploy repo now lives in the temp
    directory and dist/ contains nothing but the published site.
    """
    if not out.exists():
        return
    for root, dirs, files in os.walk(out, topdown=False):
        for f in files:
            p = os.path.join(root, f)
            try:
                os.chmod(p, 0o666)
            except OSError:
                pass
            try:
                os.unlink(p)
            except OSError:
                pass
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))
            except OSError:
                pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="build_site.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--check", action="store_true", help="audit only, write nothing")
    ap.add_argument("--keep-state", action="store_true",
                    help="ALSO publish web/data/state (not recommended: leaks internal state)")
    args = ap.parse_args(argv)

    out = Path(args.out)
    report = {"out": str(out), "copied": [], "skipped": [], "bytes": 0}

    def record(p: Path, kind: str):
        """Record an artifact. `p` is a SOURCE path, so relative names are taken
        against web/ (its source root), not against dist/."""
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        report["bytes"] += size
        try:
            rel = str(p.relative_to(WEB)).replace("\\", "/")
        except ValueError:
            rel = p.name
        report["copied"].append({"path": rel, "bytes": size, "kind": kind})

    if not args.check:
        if out.exists():
            # Only ever clear our own previous output, never an arbitrary directory.
            marker = out / ".future-site"
            if out.name == "dist" or marker.exists():
                clear_output(out)
            else:
                print(f"[abort] {out} exists and is not a previous build of this script "
                      f"(no .future-site marker). Refusing to touch it.", file=sys.stderr)
                return 1
        out.mkdir(parents=True, exist_ok=True)
        (out / ".future-site").write_text("generated by scripts/build_site.py\n", "utf-8")

    # --- index.html ---------------------------------------------------------
    idx = WEB / "index.html"
    if not idx.exists():
        print("web/index.html missing", file=sys.stderr)
        return 1
    if not args.check:
        shutil.copy2(idx, out / "index.html")
    record(idx, "entry")

    # --- front-end assets ---------------------------------------------------
    for d in COPY_DIRS:
        src = WEB / d
        if not src.exists():
            report["skipped"].append(f"{d}/ (missing)")
            continue
        for p in sorted(src.rglob("*")):
            if not p.is_file():
                continue
            if any(part in DENY_DIRS for part in p.parts):
                report["skipped"].append(str(p.relative_to(WEB)))
                continue
            if p.name.endswith(DENY_ASSET_SUFFIX):
                report["skipped"].append(str(p.relative_to(WEB)) + " (source, not shipped)")
                continue
            if not args.check:
                dst = out / p.relative_to(WEB)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
            record(p, "asset")

    # --- public data files --------------------------------------------------
    if not args.check:
        (out / "data").mkdir(parents=True, exist_ok=True)
    for name in DATA_FILES:
        src = DATA / name
        if not src.exists():
            report["skipped"].append(f"data/{name} (missing)")
            continue
        if src.name in DENY_NAMES:
            report["skipped"].append(f"data/{name} (denylist)")
            continue
        if not args.check:
            shutil.copy2(src, out / "data" / name)
        record(src, "data")

    # --- public data dirs ---------------------------------------------------
    for d in DATA_DIRS:
        src = DATA / d
        if not src.exists():
            continue
        for p in sorted(src.rglob("*")):
            if not p.is_file():
                continue
            if any(part in DENY_DIRS for part in p.relative_to(DATA).parts):
                report["skipped"].append(f"data/{p.relative_to(DATA)} (denied dir)")
                continue
            if p.name in DENY_NAMES or p.name.endswith(DENY_SUFFIX):
                report["skipped"].append(f"data/{p.relative_to(DATA)} (denylist)")
                continue
            if d == "logs" and p.name.startswith(DENY_LOG_PREFIX):
                report["skipped"].append(f"data/{p.relative_to(DATA)} (machine-local log)")
                continue
            if not args.check:
                dst = out / "data" / p.relative_to(DATA)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
            record(p, f"data/{d}")

    # --- optional state (off by default) -----------------------------------
    if args.keep_state:
        src = DATA / "state"
        for p in sorted(src.rglob("*")) if src.exists() else []:
            if p.is_file():
                if not args.check:
                    dst = out / "data" / p.relative_to(DATA)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dst)
                record(p, "state (opt-in)")

    # --- hosting niceties ---------------------------------------------------
    if not args.check:
        # Jekyll ignores files/dirs starting with "_" on GitHub Pages; this
        # opt-out keeps data/ and friends in the published output.
        (out / ".nojekyll").write_text("", "utf-8")
        # 404 -> index so a deep hash link still loads the app.
        shutil.copy2(idx, out / "404.html")
        # Security headers. Honoured by Cloudflare Pages and Netlify; GitHub Pages
        # ignores the file (it cannot set headers) - see README for that caveat.
        #
        # The app is fully self-contained (no CDN, no external fonts, no beacons),
        # so a strict CSP costs nothing:
        #   script-src 'self' 'unsafe-inline'  -> assets/js/app.js plus the one
        #                                          inline SVG sprite in index.html
        #   style-src  'self' 'unsafe-inline'  -> theme.css/app.css plus inline
        #                                          style="" used by the components
        #   connect-src 'self'                 -> fetch of web/data/*.json only
        #   img-src 'self' data:               -> no remote images are loaded
        # add_header plus a few hardening headers.
        (out / "_headers").write_text(
            "/*\n"
            "  Content-Security-Policy: default-src 'none'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'; object-src 'none'\n"
            "  X-Content-Type-Options: nosniff\n"
            "  Referrer-Policy: no-referrer\n"
            "  X-Frame-Options: DENY\n"
            "  Permissions-Policy: geolocation=(), camera=(), microphone=(), interest-cohort=()\n"
            "\n"
            "/data/*\n"
            "  Cache-Control: public, max-age=300\n"
            "\n"
            "/assets/*\n"
            "  Cache-Control: public, max-age=86400\n",
            "utf-8")
        # robots: a personal workbench has no business being crawled.
        (out / "robots.txt").write_text(
            "User-agent: *\nDisallow: /\n", "utf-8")

    # --- audit --------------------------------------------------------------
    leaked = []
    if not args.check:
        for p in out.rglob("*"):
            if p.is_file() and (p.name in DENY_NAMES or any(part in DENY_DIRS for part in p.parts)):
                leaked.append(str(p.relative_to(out)))

    manifest = read_json(DATA / "manifest.json") or {}
    m_sum = manifest.get("summary") or {}
    print("")
    print("=" * 74)
    print("dist 站点构建" + ("（--check：未写入）" if args.check else ""))
    print("=" * 74)
    print(f"  输出目录        : {out}")
    print(f"  文件数 / 体积   : {len(report['copied'])} / {human(int(report['bytes']))}")
    print(f"  数据条目        : {manifest.get('totalItems', '?')} 条 · "
          f"新增 {manifest.get('newItems', '?')} · 状态 {manifest.get('status', '?')}")
    if m_sum.get("headline"):
        print(f"  今日速览        : {m_sum['headline'][:66]}")
    print(f"  已排除          : {len(report['skipped'])} 项"
          f"（state / inbox / 本地日志 / tmp）")
    if report["skipped"]:
        for s in report["skipped"][:6]:
            print(f"      - {s}")
        if len(report["skipped"]) > 6:
            print(f"      …另外 {len(report['skipped']) - 6} 项")
    if leaked:
        print(f"  [ERROR] 仍存在敏感文件：{leaked}")
    else:
        print("  隐私检查        : 通过（state / inbox / harness 日志均未进入 dist）")
    print("=" * 74)
    print("")
    print("本地预览：")
    print(f"  python -m http.server 8790 --directory {out}")
    print("发布（任选其一，都是免费静态托管）：")
    print("  · GitHub Pages：把 dist/ 推到仓库的 gh-pages 分支，或 /docs 目录")
    print("  · Cloudflare Pages / Vercel / Netlify：构建命令留空，发布目录填 dist")
    print("  注意：站点是纯静态的，所有数据随 dist 一起发布；本机采集后重新构建即可更新。")
    print("")

    return 1 if leaked else 0


if __name__ == "__main__":
    sys.exit(main())
