#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Future 求职学习工作台 — 每日联网调研采集器（确定性层）
================================================================================
Why this file exists
  A daily workbench needs a *boring*, dependable half: something that runs at
  20:00 Asia/Shanghai, hits public endpoints, normalizes, de-duplicates, scores
  and stores — with no LLM in the loop, no third-party packages, and no single
  channel able to break the run. The creative half (summarising, formula
  breakdowns, concept explanations) is a separate, optional Agent pass driven by
  scripts/daily-agent.md.

Pipeline
  load config -> fan out over channels (thread pool, per-channel timeout+retry)
  -> normalize -> classify -> score -> dedupe (id / canonical url / title / simhash)
  -> merge into state -> atomically write digest + index + manifest + logs

Reliability rules (why the code looks the way it does)
  * A dead endpoint must not mean a dead channel. Every fixed channel tries its
    primary URL first and then the verified alternatives in `FALLBACKS`, in
    order, and logs which source actually served the data. `CHANNEL_SPECS[..]
    ["backup"]` mirrors those ids so the run log and web/data/sources.json tell
    the reader that a channel has a second home.
  * A channel that is CONFIRMED impossible (bot challenge, login wall, retired
    API, robots.txt) is listed in `DISABLED_CHANNELS` together with the evidence
    that killed it. The runner then records `status: "blocked"` plus a
    `riskNote` instead of re-hammering the host on every run. The collector
    function stays in place, so re-enabling is a one-line change; `--only id`
    still runs a blocked channel on purpose, for re-testing.
  * `parse_error` is deliberately a different status from `empty`: "the host
    answered and we could not read it" is a bug on our side, "the host answered
    with nothing" is not. Neither can abort the run.

What it deliberately does NOT do
  * No scraping of login-walled sites (Xiaohongshu, BOSS Zhipin, Lagou,
    Shixiseng). Those are handled by a human export -> import flow; see
    `manualChannels` in web/data/sources.json and the `inbox` channel.
  * No ignoring robots.txt. Reddit sits in DISABLED_CHANNELS purely because
    https://www.reddit.com/robots.txt answers `User-agent: * / Disallow: /` —
    even though its .rss feed is technically reachable.
  * No fabrication. Summaries come only from the source payload
    (title / abstract / README / description). If there is no source URL the
    item is dropped.

Usage
  python scripts/collect.py                  # full run, writes web/data
  python scripts/collect.py --only arxiv,hn  # subset, for debugging
  python scripts/collect.py --limit 20       # cap per channel
  python scripts/collect.py --dry-run        # print only, write nothing
  python scripts/collect.py --probe          # reachability table only, writes nothing
  python scripts/collect.py --init-config    # write a config template
  python scripts/collect.py -v               # per-item trace

Outputs (all under web/data, all UTF-8 without BOM)
  digest/YYYY-MM-DD.json   digest/today.json   items/index.json   items.json
  manifest.json            logs/runs.json      sources.json       state/collector-state.json
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import html
import json
import os
import random
import re
import socket
import ssl
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

COLLECTOR_VERSION = "1.1.0"
CST = timezone(timedelta(hours=8))          # Asia/Shanghai has no DST
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 FutureWorkbench/1.0 "
      "(+personal academic research; contact: local)")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "collector.config.json"
DATA_DIR = ROOT / "web" / "data"
LOGS_DIR = DATA_DIR / "logs"
DIGEST_DIR = DATA_DIR / "digest"
INDEX_DIR = DATA_DIR / "items"
STATE_PATH = DATA_DIR / "state" / "collector-state.json"
RUNS_PATH = LOGS_DIR / "runs.json"


def now_cst() -> datetime:
    return datetime.now(CST)


def iso(dt: datetime) -> str:
    return dt.astimezone(CST).isoformat(timespec="seconds")


def date_key(dt: datetime) -> str:
    return dt.astimezone(CST).strftime("%Y-%m-%d")


# ============================================================================
# 1. TAXONOMY — mirrors the front-end CATEGORIES; keywords are tunable in config
# ============================================================================
DEFAULT_CATEGORIES = {
    # `unclassified` is the bucket classify() returns when NOTHING matched. It is a
    # first-class category here (weight 0, no keywords) so that:
    #   · taxonomy.json describes it, and selfcheck stops flagging 85 items as
    #     "using a category not in taxonomy.json" - noise that hid real warnings;
    #   · it can be filtered and counted on purpose.
    # It has no keywords by design: adding any would defeat its purpose.
    "unclassified": {
        "zh": "未分类", "en": "Unclassified", "weight": 0.0,
        "desc": "没有命中任何分类关键词的条目。堆在这里通常意味着关键词需要调整。",
        "goal": "保持接近零；持续增长说明关键词覆盖不足或出现了新方向。",
        "keywords": [], "arxiv": []},
    "multimodal": {
        "zh": "多模态算法", "en": "Multimodal", "weight": 1.00,
        "desc": "视觉-语言对齐、VLM 架构、跨模态检索与生成、多模态评测。",
        "goal": "每日捕获多模态大模型的新架构、对齐方法与评测基准进展。",
        "keywords": ["multimodal", "multi-modal", "vision-language", "vlm", "mllm", "lvlm",
                     "clip", "blip", "llava", "qwen-vl", "internvl", "video-llm", "image-text",
                     "visual instruction", "cross-modal", "audio-visual", "omni-modal",
                     "segment anything", "visual tokenizer", "多模态", "视觉语言", "图文"],
        "arxiv": ["cs.CV", "cs.CL", "cs.MM"]},
    "posttraining": {
        "zh": "后训练", "en": "Post-training", "weight": 1.00,
        "desc": "SFT、RLHF、DPO/GRPO、RLVR、拒绝采样、蒸馏与偏好优化。",
        "goal": "跟踪后训练算法迭代与工程 trick，沉淀可复现的配方。",
        "keywords": ["post-training", "post training", "sft", "instruction tuning", "rlhf",
                     "dpo", "ipo", "kto", "orpo", "grpo", "rlvr", "reward model",
                     "preference optimization", "rejection sampling", "distillation",
                     "self-play", "verifier", "process reward", "reasoning model",
                     "后训练", "指令微调", "偏好优化", "蒸馏"],
        "arxiv": ["cs.CL", "cs.LG", "cs.AI"]},
    "worldmodel": {
        "zh": "世界模型", "en": "World Model", "weight": 0.95,
        "desc": "基于模型的强化学习、视频世界模型、VLA、3DGS/NeRF 场景表示。",
        "goal": "跟踪世界模型与具身智能的建模与评测路线。",
        "keywords": ["world model", "world-model", "model-based rl", "dreamer", "genie",
                     "vla", "vision-language-action", "embodied", "robot learning", "sim2real",
                     "3d gaussian", "gaussian splatting", "nerf", "neural radiance",
                     "interactive environment", "latent dynamics", "world simulator",
                     "世界模型", "具身", "机器人"],
        "arxiv": ["cs.LG", "cs.RO", "cs.CV", "cs.AI"]},
    "generative": {
        "zh": "生成式模型", "en": "Generative", "weight": 0.95,
        "desc": "Diffusion、Flow Matching、自回归生成、视频/3D 生成。",
        "goal": "掌握生成建模范式的原理演进与采样加速技术。",
        "keywords": ["diffusion", "flow matching", "rectified flow", "consistency model",
                     "ddpm", "ddim", "text-to-image", "text-to-video", "video generation",
                     "image generation", "autoregressive generation", "3d generation",
                     "dit", "adaln", "生成式", "扩散模型", "视频生成"],
        "arxiv": ["cs.CV", "cs.LG"]},
    "rl": {
        "zh": "强化学习", "en": "Reinforcement Learning", "weight": 0.85,
        "desc": "PPO/GAE、GRPO、离线 RL、探索与信用分配。",
        "goal": "打牢 RL 数学基础，并连接到 LLM 后训练实践。",
        "keywords": ["reinforcement learning", "ppo", "gae", "actor-critic", "q-learning",
                     "offline rl", "exploration", "credit assignment", "mcts", "bandit",
                     "policy gradient", "强化学习", "策略梯度"],
        "arxiv": ["cs.LG", "cs.AI"]},
    "agent": {
        "zh": "Agent 理论与实践", "en": "Agents", "weight": 0.90,
        "desc": "工具调用、规划、记忆、多智能体协作、Agent 评测与安全。",
        "goal": "理解 Agent 系统设计模式并动手实现最小可用体。",
        "keywords": ["agent", "agentic", "tool use", "tool calling", "function calling",
                     "multi-agent", "planning", "memory", "react", "reflexion", "workflow",
                     "computer use", "browser agent", "agent benchmark", "mcp",
                     "agent 框架", "智能体"],
        "arxiv": ["cs.AI", "cs.CL", "cs.SE"]},
    "engineering": {
        "zh": "工程与系统", "en": "Engineering", "weight": 0.70,
        "desc": "分布式训练、显存优化、推理加速、量化、CUDA 与并行策略。",
        "goal": "把算法想法落到可训练、可推理的工程实现上。",
        "keywords": ["inference", "serving", "vllm", "sglang", "quantization", "kv cache",
                     "flash attention", "distributed training", "fsdp", "deepspeed",
                     "megatron", "cuda", "tensorrt", "throughput", "latency",
                     "推理加速", "分布式训练", "显存"],
        "arxiv": ["cs.DC", "cs.LG"]},
    "foundation": {
        "zh": "基础理论", "en": "Foundations", "weight": 0.60,
        "desc": "线性代数、概率统计、优化、Transformer 与深度学习原理。",
        "goal": "补齐面试八股背后的第一性原理。",
        "keywords": ["transformer", "attention", "position encoding", "rope", "alibi",
                     "normalization", "rmsnorm", "layernorm", "optimizer", "scaling law",
                     "generalization", "theory", "architecture", "基础", "理论", "缩放定律"],
        "arxiv": ["cs.LG", "stat.ML"]},
    "job": {
        "zh": "岗位与招聘", "en": "Jobs & Hiring", "weight": 0.62,
        "desc": "实时招聘信息、岗位 JD、面试流程、薪资带宽与投递节奏。",
        "goal": "每日刷新在招岗位与投递窗口，不漏机会。",
        "keywords": ["招聘", "实习", "算法工程师", "校招", "社招", "jd", "intern",
                     "internship", "hiring", "recruit", "opening", "career", "岗位"],
        "arxiv": []},
    "coding": {
        "zh": "面试手撕题", "en": "Coding Interviews", "weight": 0.95,
        "desc": "手写注意力、损失函数、采样、经典算法与数据结构。",
        "goal": "形成高频手撕题的肌肉记忆与讲解话术。",
        "keywords": ["leetcode", "手撕", "面经", "算法题", "cracking", "interview question",
                     "system design", "coding interview", "八股", "笔试"],
        "arxiv": []},
    "exam": {
        "zh": "笔试场景题", "en": "Written Exams", "weight": 0.75,
        "desc": "概率题、场景设计题、工程权衡题、选择题考点。",
        "goal": "覆盖笔试与面试中的开放场景题。",
        "keywords": ["笔试", "场景题", "概率题", "选择题", "written test", "oa", "quiz"],
        "arxiv": []},
    "paper": {
        "zh": "论文与前沿", "en": "Papers", "weight": 0.90,
        "desc": "arXiv 新论文、CCF-A 顶会论文、技术报告与综述。",
        "goal": "按方向过滤高相关新论文并给出可读摘要。",
        "keywords": ["paper", "arxiv", "preprint", "survey", "benchmark", "state-of-the-art",
                     "论文", "综述", "顶会"],
        "arxiv": ["cs.CV", "cs.CL", "cs.LG"]},
    "course": {
        "zh": "课程与仓库", "en": "Courses & Repos", "weight": 0.70,
        "desc": "系统课程、开源仓库、教程与学习资源。",
        "goal": "维护固定仓库与课程的进度更新。",
        "keywords": ["tutorial", "course", "awesome", "handbook", "cookbook", "guide",
                     "implementation", "from scratch", "教程", "课程", "仓库"],
        "arxiv": []},
    "trend": {
        "zh": "技术演进与趋势", "en": "Trends", "weight": 0.65,
        "desc": "技术路线演进、行业讨论、社区热点与方法论。",
        "goal": "把零散信息串成可讲述的技术演进叙事。",
        "keywords": ["roadmap", "trend", "state of", "review", "opinion", "discussion",
                     "趋势", "演进", "复盘", "讨论"],
        "arxiv": []},
}

# ============================================================================
# 2. CHANNEL REGISTRY — tier drives scoring weight and run priority
# ============================================================================
CHANNEL_SPECS = {
    "arxiv":          {"tier": "P0", "mode": "api",    "timeout": 30, "retries": 2, "backup": []},
    "hf_papers":      {"tier": "P0", "mode": "api",    "timeout": 30, "retries": 2, "backup": ["hf_blog_rss"]},
    "hf_models":      {"tier": "P1", "mode": "api",    "timeout": 25, "retries": 1, "backup": []},
    "github":         {"tier": "P0", "mode": "api",    "timeout": 30, "retries": 2, "backup": ["gh_trending"]},
    "gh_trending":    {"tier": "P1", "mode": "html",   "timeout": 25, "retries": 1, "backup": ["github_rest_search"]},
    "openreview":     {"tier": "P0", "mode": "api",    "timeout": 30, "retries": 2, "backup": ["s2"]},
    "s2":             {"tier": "P1", "mode": "api",    "timeout": 30, "retries": 1, "backup": ["openalex", "crossref"]},
    "hn":             {"tier": "P1", "mode": "api",    "timeout": 25, "retries": 2, "backup": []},
    "reddit":         {"tier": "P2", "mode": "rss",    "timeout": 25, "retries": 1, "backup": []},
    "hf_blog_rss":    {"tier": "P2", "mode": "rss",    "timeout": 25, "retries": 1, "backup": []},
    "openai_rss":     {"tier": "P2", "mode": "rss",    "timeout": 25, "retries": 1, "backup": []},
    "machineheart":   {"tier": "P1", "mode": "rss",    "timeout": 20, "retries": 1, "backup": ["leiphone"]},
    "leiphone":       {"tier": "P1", "mode": "rss",    "timeout": 25, "retries": 1, "backup": ["qbitai"]},
    "qbitai":         {"tier": "P1", "mode": "rss",    "timeout": 20, "retries": 1, "backup": ["leiphone"]},
    "rsshub":         {"tier": "P2", "mode": "rss",    "timeout": 25, "retries": 1, "backup": ["zhihu"]},
    "nowcoder":       {"tier": "P1", "mode": "html",   "timeout": 25, "retries": 1, "backup": []},
    # 牛客「试题广场」编程题/算法题 via the sitemap the site declares in robots.txt.
    #
    # WHY a separate channel: measured, the corpus had 0.2% algorithm problems and
    # 0.6% fundamentals against 62% research papers, because every other source is a
    # paper or code feed. The reader explicitly wants 算法题/手撕/八股 to accumulate,
    # and no amount of scoring can create material that was never collected.
    #
    # COMPLIANCE: robots.txt disallows only /search, /nccommon and /ab/ab-test-flow.
    # This channel reads the DECLARED sitemap (sitemap/question/sitemap*.xml) and the
    # questionTerminal pages it points at - i.e. exactly what the site tells crawlers
    # to index - and it does not touch the disallowed paths.
    "nowcoder_questions": {"tier": "P1", "mode": "html", "timeout": 25, "retries": 1, "backup": []},
    # 代码随想录 —— 算法题解 / LeetCode Hot100 / 大模型。robots.txt 明确 Allow: / 且声明
    # sitemap，所以走 sitemap 抓取正是站点对爬虫的期望。内容不过期（2021 年的 KMP 讲解
    # 今天依然正确），因此不按发布时间设门槛。
    "programmercarl": {"tier": "P0", "mode": "html", "timeout": 30, "retries": 1, "backup": []},
    # 卡码笔记 —— 大模型/Java/C++ 八股 + 大厂面经。八股是语料里最薄的一类（1.9%），
    # 而这个站点就是为此存在的。robots.txt 同样是 Allow: / + 声明 sitemap。
    "kamacoder_notes": {"tier": "P0", "mode": "html", "timeout": 30, "retries": 1, "backup": []},
    # GitHub 算法/面试仓库（题解不过期，故不做时间过滤）。与通用 github 渠道区分：
    # 后者按 trend 取项目，产出的是 project 类，从来不是算法题。
    "github_algo": {"tier": "P0", "mode": "api", "timeout": 25, "retries": 1, "backup": []},
    # zhihu: DISABLED.
    #
    # The only reachable feed is a general hot list (mirror rss.injahow.cn), and a
    # general hot list has nothing to do with algorithm internships. Audit of 41
    # ingested items: film reviews, a phone review, a fraud news story and an
    # entertainment question were keyword-classified into posttraining / rl /
    # foundation, i.e. it was actively polluting the technical categories.
    # The targeted alternative is `nowcoder` (面经/招聘) plus the official blogs.
    # To fetch specific Zhihu questions instead, ingest their URLs by hand into
    # web/data/inbox/ - that keeps the signal and drops the firehose.
    "zhihu":          {"tier": "P2", "mode": "rss",    "timeout": 25, "retries": 1,
                       "backup": [], "disabled": True},
    # Human-in-the-loop import for login-walled channels.
    #
    # Disabled by default on purpose: login-channel collection is paused for now
    # (the reader will provide exports later). This channel never touches the
    # network either way - it only reads LOCAL files from web/data/inbox/ - but
    # keeping it off means a daily run does not imply any login-channel work is
    # happening, which is the honest default.
    # Re-enable per run:  python scripts/collect.py --only inbox
    # Re-enable permanently: "enableInbox": true in config/collector.config.json
    "inbox":          {"tier": "P0", "mode": "local",  "timeout": 5,  "retries": 0,
                       "backup": [], "disabled": True},
    "jobs_bytedance": {"tier": "P0", "mode": "api",    "timeout": 30, "retries": 2, "backup": []},
    "jobs_tencent":   {"tier": "P0", "mode": "api",    "timeout": 30, "retries": 2, "backup": []},
    "jobs_alibaba":   {"tier": "P1", "mode": "api",    "timeout": 30, "retries": 1, "backup": []},
    "jobs_zhipu":     {"tier": "P2", "mode": "html",   "timeout": 25, "retries": 1, "backup": []},
    "jobs_moonshot":  {"tier": "P2", "mode": "html",   "timeout": 25, "retries": 1, "backup": []},
    "jobs_deepseek":  {"tier": "P2", "mode": "html",   "timeout": 25, "retries": 1, "backup": ["moka_high_flyer"]},
    "jobs_minimax":   {"tier": "P2", "mode": "html",   "timeout": 25, "retries": 1, "backup": []},
    "jobs_shailab":   {"tier": "P2", "mode": "html",   "timeout": 25, "retries": 1, "backup": []},
    # login-walled: never scraped automatically
    "xiaohongshu":    {"tier": "P2", "mode": "manual", "timeout": 0,  "retries": 0, "backup": [], "auth": True},
    "boss":           {"tier": "P1", "mode": "manual", "timeout": 0,  "retries": 0, "backup": [], "auth": True},
    "lagou":          {"tier": "P2", "mode": "manual", "timeout": 0,  "retries": 0, "backup": [], "auth": True},
    "shixiseng":      {"tier": "P2", "mode": "manual", "timeout": 0,  "retries": 0, "backup": [], "auth": True},
}

# Channels that are CONFIRMED impossible from a script. Every reason below was
# observed with a real request (see research/channel_probe.json and
# scripts/probe-channels.py). The runner records them as `status: "blocked"`
# with the matching `riskNote` and does NOT spend a request on them; the
# collector functions are kept so re-enabling is one line. `--only <id>` still
# runs a disabled channel on purpose, because re-testing is how we notice that
# an upstream situation changed.
DISABLED_CHANNELS = {
    "openreview":
        "Bot challenge: api2 AND api1 /notes both answer 200 text/html "
        "'Verifying your browser | OpenReview' (earlier runs saw 403 "
        "ChallengeRequiredError). Only a browser can pass it; arXiv + s2 cover papers.",
    "reddit":
        "robots.txt: https://www.reddit.com/robots.txt is 'User-agent: * / Disallow: /', "
        "so we do not scrape it even though <sub>/top/.rss answers 200. No substitute "
        "either: lobste.rs has the same blanket Disallow. HN already covers the signal.",
    "machineheart":
        "Feed retired: /rss and /feed both answer 200 text/html (机器之心·数据服务 landing "
        "page) instead of RSS, /rss/articles is 404, and the only reachable RSSHub mirror "
        "404s on /jiqizhixin/daily. Replaced by the `leiphone` channel (雷锋网 AI feed).",
    "rsshub":
        "Every public instance is down or blocking: rsshub.app 403 Cloudflare challenge, "
        "rsshub.rssforever.com 503, rsshub.ktachibana.party 404/503, rsshub.pseudoyu.com TLS "
        "error; the single reachable mirror (rss.injahow.cn) serves only /zhihu/hotlist and "
        "/solidot, and the zhihu route is already consumed by the fixed `zhihu` channel.",
    "jobs_bytedance":
        "Edge/WAF blocks scripted clients: GET /api/v1/search/job/posts answers 200 with the "
        "HTML page 字节跳动猎头平台, POST with the documented JSON body answers 405 (0 bytes), "
        "POST with a trailing slash answers 307, /referral/api/... is 404. The JS bundle "
        "confirms the path is right, so this is the edge, not our request. Needs a browser.",
    "jobs_alibaba":
        "Anti-bot gated: the real API is POST https://talent.alibaba.com/position/search "
        "(GET -> 405, POST without CSRF -> 403). With the page's own XSRF-TOKEN replayed as "
        "?_csrf= it answers 200 but {\"success\":true,\"datas\":null,\"totalCount\":0} for "
        "every payload shape, and /searchCondition/list returns searchItems=null: the backend "
        "wants a token only the baxia browser script can mint. Needs a browser.",
    "jobs_zhipu":
        "No machine-readable listing: zhipuai.cn/joinus (200, 783KB) server-renders job "
        "*categories* (算法/校招 labels) but no per-job data, exposes no ATS host, and none of "
        "its 15 Next.js chunks reference a job API. Needs a browser or a human import.",
    "jobs_moonshot":
        "No machine-readable listing: www.moonshot.cn/careers (200, 95KB) contains zero job "
        "keywords and now delegates to careers.kimi.com, which is an 11.6KB JS shell with no "
        "embedded data (/jobs and /positions are 404) and no job API in its chunks.",
    "jobs_minimax":
        "No machine-readable listing: minimaxi.com/careers embeds a Feishu ATSX portal "
        "(vrfi1sk8a0.jobs.feishu.cn). Its index page carries no job data, its API answers 405 "
        "like ByteDance's (same ATSX edge), and minimaxi.com/robots.txt disallows /api/. "
        "Needs a browser or a human import.",
    "jobs_shailab":
        "No machine-readable listing: /joinus, /joinus/social and /joinus/campus return the "
        "same 70KB marketing page with no job rows; the openings are fetched by JS and no "
        "ATS/API endpoint is referenced. Needs a browser or a human import.",
}

CHANNEL_NAMES = {
    "arxiv": "arXiv 论文", "hf_papers": "HF 每日论文", "hf_models": "HF 模型",
    "github": "GitHub 搜索", "gh_trending": "GitHub 趋势", "openreview": "OpenReview",
    "s2": "Semantic Scholar", "hn": "Hacker News", "reddit": "Reddit",
    "machineheart": "机器之心", "qbitai": "量子位", "leiphone": "雷锋网 AI", "hf_blog_rss": "HF Blog",
    "openai_rss": "OpenAI News", "rsshub": "RSSHub", "nowcoder": "牛客网", "zhihu": "知乎热榜",
    "jobs_bytedance": "字节招聘", "jobs_tencent": "腾讯招聘", "jobs_alibaba": "阿里招聘",
    "jobs_zhipu": "智谱招聘", "jobs_moonshot": "月之暗面", "jobs_deepseek": "DeepSeek",
    "jobs_minimax": "MiniMax", "jobs_shailab": "上海 AI Lab",
    "inbox": "人工导入",
    "xiaohongshu": "小红书", "boss": "BOSS直聘", "lagou": "拉勾", "shixiseng": "实习僧",
}

MANUAL_ONLY = {cid for cid, s in CHANNEL_SPECS.items() if s.get("auth") or s["mode"] == "manual"}

# Hosts we expect to see in the knowledge base. Anything else is not rejected
# (a legitimate new source may appear) but is reported in the daily proposals
# file so a mis-parsed page cannot silently pollute the corpus unnoticed.
# Mirrors the list in scripts/selfcheck.py on purpose: the collector proposes,
# the self-check verifies, and a human decides.
KNOWN_HOSTS = {
    "arxiv.org", "export.arxiv.org", "huggingface.co", "github.com", "githubusercontent.com",
    "api.github.com", "openreview.net", "api2.openreview.net", "semanticscholar.org",
    "api.semanticscholar.org", "openalex.org", "api.openalex.org", "crossref.org",
    "doi.org", "news.ycombinator.com", "reddit.com", "jiqizhixin.com", "qbitai.com",
    "leiphone.com", "infoq.cn", "openai.com", "deepmind.google", "blog.google",
    "microsoft.com", "nvidia.com", "anthropic.com", "rsshub.app", "rss.injahow.cn",
    "nowcoder.com", "zhihu.com", "v2ex.com", "aclanthology.org", "paperswithcode.com",
    "papers.cool", "alphaxiv.org", "leetcode.cn", "leetcode.com", "codeforces.com",
    "jobs.bytedance.com", "careers.tencent.com", "talent.alibaba.com", "zhipuai.cn",
    "moonshot.cn", "kimi.com", "deepseek.com", "talent.deepseek.com", "minimaxi.com",
    "shlab.org.cn", "sensetime.com", "youtube.com", "bilibili.com", "twitter.com", "x.com",
}

DEFAULT_CONFIG = {
    "collectorVersion": COLLECTOR_VERSION,
    "targetDate": "2027-04-01",
    "targetLabel": "目标投递窗口（2027 届春招）",
    "interests": {"multimodal": 1.0, "posttraining": 1.0, "worldmodel": 0.95,
                  "generative": 0.9, "rl": 0.8, "agent": 0.85},
    "http": {"userAgent": UA, "defaultTimeout": 25, "maxRetries": 2,
             "backoffBase": 1.6, "jitter": 0.35, "delayBetween": 0.4},
    "limits": {"perChannel": 30, "maxNewPerRun": 400},
    "scoring": {"freshnessHalfLifeDays": 7.0,
                "sourceTierWeight": {"P0": 1.0, "P1": 0.85, "P2": 0.7},
                "qualityBoost": 8.0, "minRelevance": 12.0},
    "dedupe": {"simhashBits": 64, "hammingThreshold": 3},
    "arxiv": {"queryTerms": [], "maxPerQuery": 25, "lookbackDays": 10},
    "github": {"queries": [], "minStars": 40, "lookbackDays": 30},
    "categories": {},
    "retention": {"archiveAfterDays": 90, "keepDayFiles": 400},
}


# ============================================================================
# 3. UTILITIES
# ============================================================================
def safe_print(text: str = "") -> None:
    """Print without ever raising UnicodeEncodeError.

    WHY: the collector prints Chinese channel names and item titles, and a
    Windows console is still often GBK. A crashing `print` in the --dry-run /
    --probe path would turn a successful run into exit code 1.
    """
    try:
        print(text, flush=True)
    except UnicodeEncodeError:                           # pragma: no cover - legacy consoles
        enc = sys.stdout.encoding or "ascii"
        try:
            sys.stdout.write(str(text).encode(enc, "replace").decode(enc, "replace") + "\n")
            sys.stdout.flush()
        except Exception:
            pass


class Logger:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.lines: list = []

    def __call__(self, msg: str, level: str = "info") -> None:
        prefix = {"info": "  ", "ok": "[ok] ", "warn": "[!] ", "err": "[x] ", "step": "== "}.get(level, "  ")
        line = f"[{now_cst().strftime('%H:%M:%S')}] {prefix}{msg}"
        self.lines.append(line)
        try:
            print(line, flush=True)
        except UnicodeEncodeError:                       # pragma: no cover - legacy consoles
            print(line.encode("utf-8", "replace").decode("ascii", "replace"), flush=True)

    def detail(self, msg: str) -> None:
        if self.verbose:
            self("  " + msg)


def js_literal_decode(lit: str) -> str:
    """Decode the body of a JS single-quoted string literal into real text.

    WHY: talent.deepseek.com inlines its job catalogue as
    `JSON.parse('{"crawledAt":...}')`; the literal uses \\' and \\" escapes that
    json.loads() cannot read on its own.
    """
    simple = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
              "'": "'", '"': '"', "\\": "\\", "/": "/"}
    out, i = [], 0
    while i < len(lit):
        c = lit[i]
        if c == "\\" and i + 1 < len(lit):
            nxt = lit[i + 1]
            if nxt == "u":
                out.append(lit[i:i + 6])
                i += 6
                continue
            out.append(simple.get(nxt, nxt))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def first_of(row: dict, keys, default=""):
    """First non-empty value among `keys` — how one normalizer serves many APIs.

    WHY: Tencent returns RecruitPostName/PostURL while ByteDance returns
    title/url. Looking up a fixed key list is what silently turned a working
    Tencent feed into an 'empty' channel before.
    """
    for k in keys:
        val = (row or {}).get(k)
        if val not in (None, "", [], {}):
            return val
    return default


def http_get(url, *, timeout=25, headers=None, accept=None, cfg=None, log=None,
             retries=2, data=None, method="GET"):
    """HTTP with exponential backoff + jitter. Returns (status, bytes, content_type)."""
    cfg = cfg or {}
    http_cfg = cfg.get("http") or DEFAULT_CONFIG["http"]
    hdrs = {
        "User-Agent": http_cfg.get("userAgent", UA),
        "Accept": accept or "application/json, text/html;q=0.9, application/xml;q=0.8, */*;q=0.7",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    }
    if headers:
        hdrs.update(headers)

    ctx = ssl.create_default_context()
    last_err = None
    for attempt in range(max(1, retries + 1)):
        try:
            req = urllib.request.Request(url, headers=hdrs, data=data, method=method)
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                body = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    body = gzip.decompress(body)
                return resp.status, body, resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (403, 408, 425, 429, 500, 502, 503, 504) and attempt < retries:
                pass
            else:
                raise
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, ConnectionError, OSError) as e:
            last_err = e
            if attempt >= retries:
                break
        base = float(http_cfg.get("backoffBase", 1.6))
        jitter = float(http_cfg.get("jitter", 0.35))
        delay = (base ** attempt) + random.uniform(0, jitter * (base ** attempt))
        if log:
            log(f"retry {attempt + 1}/{retries} in {delay:.1f}s ({type(last_err).__name__})", "warn")
        time.sleep(delay)
    raise last_err if last_err else RuntimeError("request failed")


def http_json(url, **kw):
    _status, body, _ct = http_get(url, **kw)
    return json.loads(body.decode("utf-8", "replace"))


def strip_html(s) -> str:
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", str(s))
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def clean_text(s, limit: int = 1200) -> str:
    if s is None:
        return ""
    return re.sub(r"\s+", " ", strip_html(s)).strip()[:limit]


def parse_ts(value):
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(CST)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"   # Z means UTC, not +20:00
    cn = re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日", s)          # 腾讯招聘 LastUpdateTime
    if cn:
        try:
            return datetime(int(cn.group(1)), int(cn.group(2)), int(cn.group(3)), tzinfo=CST)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s)
        return dt.astimezone(CST) if dt.tzinfo else dt.replace(tzinfo=CST)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d",
                "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.astimezone(CST) if dt.tzinfo else dt.replace(tzinfo=CST)
        except ValueError:
            continue
    return None


TRACKING_PARAMS = re.compile(
    r"^(utm_|spm|ref$|ref_|from$|source$|share_|share$|scene$|src$|fbclid|gclid|"
    r"mc_cid|mc_eid|_hsenc|_hsmi|yclid|abbucket|ivk_sa|s_r$|share_token|"
    r"loginfrom|_t$|hmsr|hmpl|hmcu|hmkw|hmci|weibo_id|timestamp$)", re.I)


def canonical_url(u: str) -> str:
    if not u:
        return ""
    u = str(u).strip()
    if u.startswith("//"):
        u = "https:" + u
    try:
        parts = urllib.parse.urlsplit(u)
    except ValueError:
        return u
    if not parts.netloc:
        return ""
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=False)
             if not TRACKING_PARAMS.match(k)]
    path = re.sub(r"(arxiv\.org/(?:abs|pdf)/\d{4}\.\d{4,5})v\d+", r"\1", parts.path.rstrip("/") or "/")
    if path.endswith(".pdf"):
        path = path[:-4]
    return urllib.parse.urlunsplit(
        (parts.scheme or "https", parts.netloc.lower().replace("www.", ""), path,
         urllib.parse.urlencode(query), ""))


def norm_title(t: str) -> str:
    if not t:
        return ""
    t = strip_html(t).lower()
    t = re.sub(r"[\s\u3000]+", " ", t)
    return re.sub(r"[^\w\u4e00-\u9fff ]+", "", t).strip()


def sha1(s: str) -> str:
    return hashlib.sha1(str(s).encode("utf-8", "replace")).hexdigest()


def stable_id(source: str, external: str) -> str:
    return sha1(f"{source}::{external}")[:16]


def title_key(t: str) -> str:
    n = norm_title(t)
    return sha1(n)[:16] if n else ""


_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


def simhash(text: str, bits: int = 64) -> int:
    tokens = _TOKEN_RE.findall((text or "").lower())
    if not tokens:
        return 0
    grams = [" ".join(tokens[i:i + 3]) for i in range(max(1, len(tokens) - 2))]
    vec = [0] * bits
    for g in grams:
        h = int(hashlib.md5(g.encode("utf-8", "replace")).hexdigest(), 16)
        for i in range(bits):
            vec[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(bits):
        if vec[i] > 0:
            out |= (1 << i)
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# ============================================================================
# 4. CHANNEL COLLECTORS — each returns a list of raw dicts; raising is allowed
#    (the runner isolates failures per channel).
# ============================================================================
ARXIV_NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def _arxiv_default_terms(cfg):
    """Build OR-queries from the most distinctive english keywords per category."""
    terms = []
    for cid, c in cfg["categories"].items():
        kws = [k for k in c.get("keywords", [])
               if len(k) > 3 and re.match(r"^[a-z0-9][a-z0-9\- ]*$", k)][:3]
        if kws:
            terms.append(" OR ".join(f'all:"{k}"' for k in kws))
    return terms[:6]


def collect_arxiv(cfg, log, limit):
    out = []
    acfg = cfg.get("arxiv", {})
    terms = acfg.get("queryTerms") or _arxiv_default_terms(cfg)
    max_q = int(acfg.get("maxPerQuery") or limit or 25)
    cutoff = now_cst() - timedelta(days=int(acfg.get("lookbackDays", 10)))
    for term in terms:
        url = ("http://export.arxiv.org/api/query?search_query=" +
               urllib.parse.quote(term, safe="") +
               f"&start=0&max_results={max_q}&sortBy=submittedDate&sortOrder=descending")
        try:
            _s, body, _ct = http_get(url, timeout=CHANNEL_SPECS["arxiv"]["timeout"],
                                     retries=CHANNEL_SPECS["arxiv"]["retries"], cfg=cfg, log=log,
                                     accept="application/atom+xml, application/xml;q=0.9, */*;q=0.8")
            root = ET.fromstring(body)
        except Exception as e:
            log(f"arxiv query failed ({term[:40]}): {e}", "warn")
            continue
        got = 0
        for entry in root.findall("a:entry", ARXIV_NS):
            title = clean_text(entry.findtext("a:title", "", ARXIV_NS), 400)
            link = (entry.findtext("a:id", "", ARXIV_NS) or "").strip()
            if not title or not link:
                continue
            published = parse_ts(entry.findtext("a:published", "", ARXIV_NS))
            updated = parse_ts(entry.findtext("a:updated", "", ARXIV_NS))
            if published and published < cutoff:
                continue
            summary = clean_text(entry.findtext("a:summary", "", ARXIV_NS), 1800)
            cats = [c.attrib.get("term", "") for c in entry.findall("a:category", ARXIV_NS)]
            prim = entry.find("arxiv:primary_category", ARXIV_NS)
            primary = prim.attrib.get("term", "") if prim is not None else (cats[0] if cats else "")
            comment = clean_text(entry.findtext("arxiv:comment", "", ARXIV_NS), 240)
            out.append({
                "sourceId": "arxiv", "channel": "arxiv",
                "externalId": link.rsplit("/", 1)[-1],
                "title": title, "summary": summary,
                "url": link.replace("http://", "https://"),
                "publishedAt": iso(published or updated or now_cst()),
                "authors": [clean_text(a.findtext("a:name", "", ARXIV_NS), 60)
                            for a in entry.findall("a:author", ARXIV_NS)][:12],
                "tags": ([primary] if primary else []) + cats[:4],
                "venue": (comment.split(".")[0][:60] if comment else "arXiv"),
                "peerReviewed": False,
                "codeAvailable": bool(re.search(r"github|code|open-?source", summary, re.I)),
                "lang": "en",
            })
            got += 1
        log.detail(f"arxiv '{term[:36]}' -> {got}")
        time.sleep(float((cfg.get("http") or {}).get("delayBetween", 0.4)))
    return out


def collect_hf_papers(cfg, log, limit):
    out = []
    data = http_json("https://huggingface.co/api/daily_papers?limit=%d" % min(max(limit, 10) * 2, 100),
                     cfg=cfg, log=log, retries=2)
    for row in data if isinstance(data, list) else []:
        p = row.get("paper") or row
        title = clean_text(p.get("title", ""), 400)
        if not title:
            continue
        pid = str(p.get("id") or title_key(title))
        up = int(p.get("upvotes") or row.get("upvotes") or 0)
        out.append({
            "sourceId": "hf_papers", "channel": "hf_papers", "externalId": pid,
            "title": title, "summary": clean_text(p.get("summary", ""), 1800),
            "url": f"https://huggingface.co/papers/{pid}",
            "canonicalUrl": (f"https://arxiv.org/abs/{pid}"
                             if re.match(r"^\d{4}\.\d{4,5}$", pid) else None),
            "publishedAt": iso(parse_ts(p.get("publishedAt") or row.get("publishedAt")) or now_cst()),
            "authors": [a.get("name", "") for a in (p.get("authors") or [])][:12],
            "upvotes": up, "qualitySignals": {"upvotes": up}, "lang": "en",
        })
    return out


def collect_hf_models(cfg, log, limit):
    """HuggingFace model releases, with a popularity floor.

    Why the floor: without it this channel was the single largest source of junk
    in the library. A corpus audit found 80 low-value cards from here - random
    community uploads such as `BabyLM-community/babylm-multimodal-baseline-git`
    (score 31) and several duplicate `...-MLX` / `...-NVF` re-uploads of the same
    uncensored model. They are technically "models" but carry no signal about the
    field. A downloads/likes threshold keeps the models people actually use.

    Only models that clear BOTH a download floor and a like floor, or are very
    recent and already popular, are accepted.
    """
    out = []
    qcfg = cfg.get("hfModels") or {}
    min_downloads = int(qcfg.get("minDownloads", 2000))
    min_likes = int(qcfg.get("minLikes", 15))
    dropped = 0

    for term in ["vision-language", "multimodal", "text-to-video", "world-model",
                 "video-generation", "vision-language-action"]:
        url = ("https://huggingface.co/api/models?search=" + urllib.parse.quote(term) +
               "&sort=downloads&direction=-1&limit=%d" % min(max(10, limit), 25) +
               "&full=false&config=false")
        try:
            data = http_json(url, cfg=cfg, log=log, retries=1)
        except Exception as e:
            log(f"hf models '{term}' failed: {e}", "warn")
            continue
        for m in data if isinstance(data, list) else []:
            mid = m.get("modelId") or m.get("id")
            if not mid:
                continue
            dl = int(m.get("downloads") or 0)
            likes = int(m.get("likes") or 0)
            if dl < min_downloads and likes < min_likes:
                dropped += 1
                continue
            out.append({
                "sourceId": "hf_models", "channel": "hf_models", "externalId": mid,
                "title": mid,
                # State the numbers so the reader can judge, and so the relevance
                # scorer has real text to work with instead of a bare identifier.
                "summary": (f"HuggingFace 模型 {mid}：下载 {dl}、点赞 {likes}。"
                            f"搜索词「{term}」。标签：{', '.join((m.get('tags') or [])[:6]) or '无'}。"
                            f"模型页含用法与权重，适合作为复现或微调的起点。"),
                "url": f"https://huggingface.co/{mid}",
                "publishedAt": iso(parse_ts(m.get("lastModified") or m.get("createdAt")) or now_cst()),
                "tags": ((m.get("tags") or [])[:10] + [term]),
                "qualitySignals": {"downloads": dl, "likes": likes},
                "codeAvailable": True,      # a model repo is deployable material
                "lang": "en",
            })
        time.sleep(0.3)
    if dropped:
        log(f"  hf_models: dropped {dropped} models below the popularity floor "
            f"(downloads<{min_downloads} and likes<{min_likes})", "warn")
    return out


def collect_github(cfg, log, limit):
    out = []
    gcfg = cfg.get("github", {})
    queries = gcfg.get("queries") or [
        "multimodal llm", "vision language model", "post-training llm", "rlhf dpo grpo",
        "world model reinforcement learning", "diffusion video generation",
        "llm agent framework", "llm inference engine",
    ]
    min_stars = int(gcfg.get("minStars", 40))
    since = (now_cst() - timedelta(days=int(gcfg.get("lookbackDays", 30)))).strftime("%Y-%m-%d")
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for q in queries[:8]:
        url = ("https://api.github.com/search/repositories?q=" +
               urllib.parse.quote(f"{q} stars:>{min_stars} pushed:>{since}") +
               "&sort=stars&order=desc&per_page=%d" % min(max(5, limit), 20))
        try:
            data = http_json(url, headers=headers, cfg=cfg, log=log,
                             retries=CHANNEL_SPECS["github"]["retries"],
                             timeout=CHANNEL_SPECS["github"]["timeout"])
        except Exception as e:
            log(f"github '{q}' failed: {e}", "warn")
            continue
        for r in (data.get("items") or []) if isinstance(data, dict) else []:
            name = r.get("full_name")
            if not name:
                continue
            desc = clean_text(r.get("description") or "", 600)
            stars = r.get("stargazers_count", 0)
            out.append({
                "sourceId": "github", "channel": "github", "externalId": name,
                "title": f"{name} - {desc}".strip(" -"),
                "summary": desc, "url": r.get("html_url"),
                "publishedAt": iso(parse_ts(r.get("pushed_at") or r.get("created_at")) or now_cst()),
                "authors": [(r.get("owner") or {}).get("login", "")],
                "tags": (r.get("topics") or [])[:8] + ([r.get("language")] if r.get("language") else []),
                "stars": stars,
                "qualitySignals": {"stars": stars, "forks": r.get("forks_count", 0), "codeAvailable": True},
                "codeAvailable": True, "lang": "en",
            })
        # unauthenticated GitHub search is limited to 10 requests/minute
        time.sleep(1.0 if token else 6.5)
    return out


def _gh_trending_from_html(text: str, limit: int):
    """Parse the trending page by CARD, because the markup carries many attributes.

    WHY the regex changed: GitHub now renders
      <h2 class="h3 lh-condensed">
        <a data-hydro-click="..." data-hydro-click-hmac="..." href="/owner/repo" ...>
    i.e. `href` is no longer the first attribute of the anchor, so the previous
    pattern `<h2 class="h3 lh-condensed">\\s*<a href="/...` matched 0 of 17 cards
    while the request itself was a perfectly healthy 200. Verified 2026-10-01:
    the card regex below matches 17/17 cards, and all 17 also expose a
    description, a total star count, a "stars today" count and a language.
    """
    out = []
    card_re = re.compile(r'<article class="Box-row">(.*?)</article>', re.S)
    repo_re = re.compile(r'<h2 class="h3 lh-condensed">\s*<a[^>]*?href="/([^"]+)"')
    desc_re = re.compile(r'<p class="col-9 color-fg-muted my-1[^"]*">\s*(.*?)\s*</p>', re.S)
    stars_re = re.compile(r'/stargazers"[^>]*>\s*(?:<[^>]+>\s*)*([\d,\.k]+)')
    today_re = re.compile(r'([\d,\.k]+)\s+stars?\s+today')
    lang_re = re.compile(r'itemprop="programmingLanguage">\s*([^<]+?)\s*<')

    def to_int(txt):
        txt = (txt or "").replace(",", "").strip().lower()
        try:
            return int(float(txt[:-1]) * 1000) if txt.endswith("k") else int(float(txt))
        except ValueError:
            return None

    for block in card_re.findall(text):
        m = repo_re.search(block)
        if not m:
            continue
        full = m.group(1).strip()
        if full.count("/") != 1:                 # trending also links orgs/topics
            continue
        desc = clean_text(desc_re.search(block).group(1), 300) if desc_re.search(block) else ""
        stars = to_int(stars_re.search(block).group(1)) if stars_re.search(block) else None
        today = to_int(today_re.search(block).group(1)) if today_re.search(block) else None
        lang = clean_text(lang_re.search(block).group(1), 40) if lang_re.search(block) else ""
        out.append({
            "sourceId": "gh_trending", "channel": "gh_trending", "externalId": full,
            "title": f"{full} - {desc}".strip(" -") if desc else f"GitHub Trending: {full}",
            "summary": desc or "项目进入今日 GitHub 趋势榜。",
            "url": f"https://github.com/{full}",
            "publishedAt": iso(now_cst()),
            "tags": [t for t in ([lang] if lang else []) + ["trending"] if t],
            "stars": stars,
            "qualitySignals": {"codeAvailable": True, "stars": stars,
                               "starsToday": today, "language": lang},
            "codeAvailable": True, "lang": "en",
        })
        if len(out) >= limit:
            break
    return out


def _gh_trending_from_rest(cfg, log, limit, lookback_days=7):
    """备用来源: GitHub REST search for repositories created in the last N days.

    Verified 2026-10-01: HTTP 200 JSON (12.6M matches for a plain date filter).
    Used only when the trending page changes shape again; unauthenticated search
    is rate limited to ~10 requests/minute, so this costs exactly one request.
    """
    since = (now_cst() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    url = ("https://api.github.com/search/repositories?q=" +
           urllib.parse.quote(f"created:>{since}") +
           "&sort=stars&order=desc&per_page=%d" % min(max(5, limit), 30))
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = http_json(url, headers=headers, cfg=cfg, log=log, retries=1, timeout=25)
    out = []
    for r in (data.get("items") or []) if isinstance(data, dict) else []:
        name = r.get("full_name")
        if not name:
            continue
        desc = clean_text(r.get("description") or "", 300)
        out.append({
            "sourceId": "gh_trending", "channel": "gh_trending", "externalId": name,
            "title": f"{name} - {desc}".strip(" -") if desc else f"GitHub Trending: {name}",
            "summary": desc or f"近 {lookback_days} 天新建的高星仓库（GitHub 趋势备用来源）。",
            "url": r.get("html_url") or f"https://github.com/{name}",
            "publishedAt": iso(parse_ts(r.get("created_at")) or now_cst()),
            "tags": (r.get("topics") or [])[:6] + ["trending-rest"],
            "stars": r.get("stargazers_count", 0),
            "qualitySignals": {"codeAvailable": True, "stars": r.get("stargazers_count", 0)},
            "codeAvailable": True, "lang": "en",
        })
        if len(out) >= limit:
            break
    return out


def collect_gh_trending(cfg, log, limit):
    """GitHub Trending, parsed from the server-rendered cards.

    Primary: https://github.com/trending?since=daily (200, server-rendered HTML,
    robots.txt's generic section does not disallow /trending).
    备用来源: the REST search endpoint above, for when the markup changes again.
    """
    limit = max(1, int(limit or 1))
    out, err = [], None
    try:
        _s, body, _ct = http_get("https://github.com/trending?since=daily", timeout=20,
                                 cfg=cfg, log=log, retries=1)
        out = _gh_trending_from_html(body.decode("utf-8", "replace"), limit)
    except Exception as e:
        err = e
        log(f"gh_trending html failed: {e}", "warn")
    if not out:
        try:
            out = _gh_trending_from_rest(cfg, log, limit)
            if out:
                log("gh_trending 使用备用来源 GitHub REST search", "warn")
        except Exception as e:
            log(f"gh_trending rest fallback failed: {e}", "warn")
            if err:
                raise err
    return out


def collect_openreview(cfg, log, limit):
    """OpenReview — DISABLED (see DISABLED_CHANNELS), code kept for reference.

    Measured 2026-10-01: api2 AND the legacy api1 `/notes` endpoints both answer
    HTTP 200 with `text/html` — the page titled "Verifying your browser |
    OpenReview" — i.e. an interstitial that only a real browser passes. Earlier
    runs saw the JSON form of the same wall (403 ChallengeRequiredError). No
    public alternative exists, and arXiv + s2 already cover the paper signal.
    """
    out = []
    for venue in ["ICLR.cc/2026/Conference", "NeurIPS.cc/2025/Conference", "ICML.cc/2025/Conference"]:
        url = ("https://api2.openreview.net/notes?content.venue=" + urllib.parse.quote(venue) +
               "&limit=%d&details=replyCount&sort=cdate:desc" % min(max(5, limit), 30))
        try:
            data = http_json(url, cfg=cfg, log=log, retries=1, timeout=30)
        except Exception as e:
            log(f"openreview '{venue}' failed: {e}", "warn")
            continue
        for n in (data.get("notes") or []) if isinstance(data, dict) else []:
            c = n.get("content") or {}
            title = clean_text((c.get("title") or {}).get("value", ""), 400)
            if not title:
                continue
            nid = str(n.get("id") or title_key(title))
            short = venue.split("/")[0]
            out.append({
                "sourceId": "openreview", "channel": "openreview", "externalId": nid,
                "title": title,
                "summary": clean_text((c.get("abstract") or {}).get("value", ""), 1800),
                "url": f"https://openreview.net/forum?id={nid}",
                "publishedAt": iso(parse_ts(n.get("cdate")) or now_cst()),
                "authors": (c.get("authors") or {}).get("value", []) or [],
                "venue": short, "peerReviewed": True,
                "qualitySignals": {"peerReviewed": True, "venue": short,
                                   "replies": n.get("replyCount", 0)},
                "tags": (c.get("keywords") or {}).get("value", []) or [],
                "lang": "en",
            })
        time.sleep(0.6)
    return out


S2_FIELDS = ("title,abstract,url,publicationDate,authors,venue,citationCount,externalIds")


def _s2_bulk(cfg, log, limit):
    """Primary S2 transport: /paper/search/bulk.

    Verified 2026-10-01: /paper/search answered HTTP 429, while
    /paper/search/bulk answered 200 with total=367696 — and 429/200/429 again
    inside the same minute, which is why `retries=2` and the two 备用来源 below
    both matter. Bulk ignores `limit` and always returns up to 1000 rows, so we
    sort by publicationDate desc, slice to `limit`, and pay one ~1-2MB body per
    day for abstracts that are actually worth scoring.
    """
    lookback = (now_cst() - timedelta(days=45)).strftime("%Y-%m-%d")
    query = '"large language model" OR multimodal OR "world model" OR "reinforcement learning"'
    url = ("https://api.semanticscholar.org/graph/v1/paper/search/bulk?query=" +
           urllib.parse.quote(query) + f"&fields={S2_FIELDS}" +
           f"&sort=publicationDate:desc&publicationDateOrYear={lookback}:")
    # retries=2 because the 429s are transient: in one measured minute the same
    # bulk URL answered 200, 429, 200. The backoff in http_get covers it, and
    # OpenAlex is still waiting as the next 备用来源 if it does not recover.
    data = http_json(url, cfg=cfg, log=log, retries=2, timeout=40)
    future_cutoff = now_cst() + timedelta(days=120)
    out = []
    for p in (data.get("data") or []) if isinstance(data, dict) else []:
        title = clean_text(p.get("title", ""), 400)
        if not title:
            continue
        stamp = parse_ts(p.get("publicationDate"))
        if stamp and stamp > future_cutoff:
            continue          # S2 carries scheduled issue dates far in the future
        ext = p.get("externalIds") or {}
        out.append({
            "sourceId": "s2", "channel": "s2",
            "externalId": str(p.get("paperId") or title_key(title)),
            "title": title, "summary": clean_text(p.get("abstract") or "", 1600),
            "url": p.get("url") or (f"https://arxiv.org/abs/{ext['ArXiv']}" if ext.get("ArXiv") else ""),
            "publishedAt": iso(stamp or now_cst()),
            "authors": [a.get("name", "") for a in (p.get("authors") or [])][:12],
            "venue": p.get("venue") or "", "peerReviewed": bool(p.get("venue")),
            "qualitySignals": {"citations": p.get("citationCount", 0), "venue": p.get("venue") or "",
                               "via": "semanticscholar-bulk"},
            "lang": "en",
        })
        if len(out) >= limit:
            break
    return out


def _openalex_abstract(inverted):
    """OpenAlex ships abstracts as a word -> positions map; rebuild the text."""
    if not isinstance(inverted, dict):
        return ""
    positions = {}
    for word, idxs in inverted.items():
        for i in idxs or []:
            positions[i] = word
    return " ".join(positions[k] for k in sorted(positions))


def _s2_openalex(cfg, log, limit):
    """备用来源 1: OpenAlex. Generous, no key, verified 200 with 25 rows."""
    url = ("https://api.openalex.org/works?search=" +
           urllib.parse.quote("multimodal large language model") +
           "&per-page=%d&sort=publication_date:desc&mailto=workbench@example.com" % min(max(5, limit), 50))
    data = http_json(url, cfg=cfg, log=log, retries=1, timeout=30,
                     headers={"Accept": "application/json"})
    out = []
    for w in (data.get("results") or []) if isinstance(data, dict) else []:
        title = clean_text(w.get("title") or w.get("display_name") or "", 400)
        if not title:
            continue
        venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
        out.append({
            "sourceId": "s2", "channel": "s2",
            "externalId": str(w.get("id") or title_key(title)),
            "title": title,
            "summary": clean_text(_openalex_abstract(w.get("abstract_inverted_index")), 1600),
            "url": w.get("doi") or w.get("id") or "",
            "publishedAt": iso(parse_ts(w.get("publication_date")) or now_cst()),
            "authors": [(a.get("author") or {}).get("display_name", "")
                        for a in (w.get("authorships") or [])][:12],
            "venue": venue, "peerReviewed": bool(venue),
            "qualitySignals": {"citations": w.get("cited_by_count", 0), "venue": venue,
                               "via": "openalex"},
            "lang": "en",
        })
    return out


def _s2_crossref(cfg, log, limit):
    """备用来源 2: Crossref. Verified 200 with 20 rows; abstract is JATS XML."""
    url = ("https://api.crossref.org/works?query=" +
           urllib.parse.quote("multimodal large language model") +
           "&rows=%d&sort=published&order=desc" % min(max(5, limit), 50) +
           "&select=DOI,title,abstract,URL,published,author,container-title,type")
    data = http_json(url, cfg=cfg, log=log, retries=1, timeout=30,
                     headers={"Accept": "application/json"})
    items = (((data or {}).get("message") or {}).get("items")) or []
    out = []
    for it in items:
        title = clean_text((it.get("title") or [""])[0], 400)
        if not title:
            continue
        published = ((it.get("published") or {}).get("date-parts") or [[None]])[0]
        stamp = None
        if published and published[0]:
            try:
                stamp = datetime(int(published[0]), int(published[1] or 1), int(published[2] or 1), tzinfo=timezone.utc)
            except (TypeError, ValueError):
                stamp = None
            if stamp and stamp > now_cst() + timedelta(days=30):
                continue                      # Crossref carries a few absurd future dates
        out.append({
            "sourceId": "s2", "channel": "s2",
            "externalId": str(it.get("DOI") or title_key(title)),
            "title": title, "summary": clean_text(it.get("abstract") or "", 1600),
            "url": it.get("URL") or (f"https://doi.org/{it['DOI']}" if it.get("DOI") else ""),
            "publishedAt": iso(stamp or now_cst()),
            "authors": [clean_text(f"{a.get('given', '')} {a.get('family', '')}", 80)
                        for a in (it.get("author") or [])][:12],
            "venue": clean_text((it.get("container-title") or [""])[0], 120),
            "peerReviewed": True,
            "qualitySignals": {"peerReviewed": True, "via": "crossref"},
            "lang": "en",
        })
    return out


def collect_s2(cfg, log, limit):
    """Semantic Scholar + ordered 备用来源 (OpenAlex, Crossref).

    The legacy /paper/search endpoint is still attempted first only when the
    caller opts in via config; by default we go straight to the endpoints that
    were verified working, because a burst of 429s every morning is noise.
    """
    for label, fn in (("semanticscholar-bulk", _s2_bulk),
                      ("openalex", _s2_openalex),
                      ("crossref", _s2_crossref)):
        try:
            rows = fn(cfg, log, max(1, int(limit or 1)))
        except Exception as e:
            log(f"s2 {label} failed: {e}", "warn")
            continue
        if rows:
            if label != "semanticscholar-bulk":
                log(f"s2 使用备用来源 {label}", "warn")
            return rows
    return []


def collect_hn(cfg, log, limit):
    out = []
    for q in ["multimodal LLM", "post-training RLHF", "world model", "diffusion model",
              "LLM agent", "GRPO", "vision language model", "inference optimization"]:
        url = ("https://hn.algolia.com/api/v1/search?query=" + urllib.parse.quote(q) +
               f"&tags=story&hitsPerPage={min(max(3, limit), 10)}&numericFilters=points>20")
        try:
            data = http_json(url, cfg=cfg, log=log, retries=1)
        except Exception as e:
            log(f"hn '{q}' failed: {e}", "warn")
            continue
        for h in (data.get("hits") or []) if isinstance(data, dict) else []:
            title = clean_text(h.get("title") or "", 300)
            if not title:
                continue
            oid = str(h.get("objectID"))
            pts = h.get("points", 0)
            out.append({
                "sourceId": "hn", "channel": "hn", "externalId": oid,
                "title": title, "summary": clean_text(h.get("story_text") or "", 800),
                "url": h.get("url") or f"https://news.ycombinator.com/item?id={oid}",
                "canonicalUrl": f"https://news.ycombinator.com/item?id={oid}",
                "publishedAt": iso(parse_ts(h.get("created_at")) or now_cst()),
                "authors": [h.get("author") or ""], "upvotes": pts,
                "qualitySignals": {"upvotes": pts, "comments": h.get("num_comments", 0)},
                "lang": "en",
            })
        time.sleep(0.3)
    return out


def collect_reddit(cfg, log, limit):
    """Reddit — DISABLED (see DISABLED_CHANNELS), code kept for re-enabling.

    Reachability as measured on 2026-10-01: the legacy JSON endpoint
    /r/<sub>/top.json answers HTTP 403, while the Atom feed
    /r/<sub>/top/.rss?t=day&limit=N answers 200 with 5 entries. We still do NOT
    collect it: https://www.reddit.com/robots.txt is `User-agent: * /
    Disallow: /`, and honouring robots.txt is a hard rule for this collector.
    lobste.rs has the same blanket Disallow, so there is no drop-in substitute
    either; Hacker News (`hn`) already carries that community signal.
    """
    out = []
    for sub in ["MachineLearning", "LocalLLaMA", "comfyui"]:
        try:
            out += _collect_rss(
                f"https://www.reddit.com/r/{sub}/top/.rss?t=day&limit={min(max(3, limit), 15)}",
                "reddit", cfg, log, min(max(3, limit), 15), [sub])
        except Exception as e:
            log(f"reddit r/{sub} failed: {e}", "warn")
        time.sleep(0.6)
    return out


def _collect_rss(url, source_id, cfg, log, limit, tags=None):
    out = []
    _s, body, _ct = http_get(url, timeout=20, cfg=cfg, log=log, retries=1,
                             accept="application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8")
    root = ET.fromstring(body)
    items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    for it in items[:limit]:

        def tx(tag, _it=it):
            el = _it.find(tag)
            if el is None:
                el = _it.find("{http://www.w3.org/2005/Atom}" + tag)
            return (el.text or "").strip() if el is not None and el.text else ""

        title = clean_text(tx("title"), 300)
        link = tx("link")
        if not link:
            le = it.find("{http://www.w3.org/2005/Atom}link")
            link = le.attrib.get("href", "") if le is not None else ""
        if not title or not link:
            continue
        out.append({
            "sourceId": source_id, "channel": source_id, "externalId": link,
            "title": title,
            "summary": clean_text(tx("description") or tx("summary") or tx("content"), 900),
            "url": link,
            "publishedAt": iso(parse_ts(tx("pubDate") or tx("published") or tx("updated")) or now_cst()),
            "tags": list(tags or []),
            "lang": "zh" if re.search(r"[\u4e00-\u9fff]", title) else "en",
        })
    return out


def collect_machineheart(cfg, log, limit):
    """机器之心 — DISABLED (see DISABLED_CHANNELS), code kept for re-enabling.

    Measured 2026-10-01: https://www.jiqizhixin.com/rss and /feed both answer
    HTTP 200 but with the HTML landing page 机器之心·数据服务 instead of XML;
    /rss/articles is 404; the only reachable RSSHub mirror 404s on
    /jiqizhixin/daily. The publisher retired the feed, so the Chinese-AI-media
    slot is served by `collect_leiphone` (雷锋网) instead.
    """
    return _collect_rss("https://www.jiqizhixin.com/rss", "machineheart", cfg, log, limit, ["资讯"])


def collect_leiphone(cfg, log, limit):
    """雷锋网 — the replacement Chinese AI/tech media feed for 机器之心.

    备用来源 requirement: 量子位 (qbitai) is the other live Chinese AI feed, and
    CHANNEL_SPECS marks it as this channel's backup. Verified 2026-10-01:
    https://www.leiphone.com/feed answers 200 application/rss+xml with 20 items,
    the newest stamped the same day, and 算法/工程师 appear across the titles.
    robots.txt disallows /dynamic/, /search/ and friends but not /feed.
    """
    return _collect_rss("https://www.leiphone.com/feed", "leiphone", cfg, log, limit,
                        ["资讯", "AI", "中文媒体"])


def collect_qbitai(cfg, log, limit):
    return _collect_rss("https://www.qbitai.com/feed", "qbitai", cfg, log, limit, ["资讯"])


def collect_hf_blog_rss(cfg, log, limit):
    return _collect_rss("https://huggingface.co/blog/feed.xml", "hf_blog_rss", cfg, log, limit, ["blog"])


def collect_openai_rss(cfg, log, limit):
    return _collect_rss("https://openai.com/news/rss.xml", "openai_rss", cfg, log, limit, ["blog"])


RSSHUB_MIRRORS = [
    # Ordered by measured health on 2026-10-01. WHY the order: rsshub.app is
    # behind a Cloudflare interstitial (403 "Just a moment...") and
    # rsshub.rssforever.com answers 503, so the community mirror is the primary.
    "https://rss.injahow.cn",
    "https://rsshub.rssforever.com",
    "https://rsshub.app",
]


def collect_rsshub(cfg, log, limit):
    """RSSHub bridge — DISABLED (see DISABLED_CHANNELS), code kept for re-enabling.

    Measured 2026-10-01: rsshub.app 403 (Cloudflare), rsshub.rssforever.com 503,
    rsshub.ktachibana.party 404/503, rsshub.pseudoyu.com TLS error. The only
    reachable mirror serves /zhihu/hotlist (already consumed by `zhihu`) and
    /solidot; every other route tested (github/trending/daily/any, 36kr, v2ex,
    hackernews, sspai) answered 503. Kept so it can be re-enabled when the
    public instances recover.
    """
    out = []
    for route in ["zhihu/hotlist", "github/trending/daily/any"]:
        for base in RSSHUB_MIRRORS:
            try:
                rows = _collect_rss(f"{base}/{route}", "rsshub", cfg, log,
                                    max(5, limit // 2), ["rsshub"])
            except Exception as e:
                log(f"rsshub {base}/{route} failed: {e}", "warn")
                continue
            if rows:
                out += rows
                break
    return out


def collect_zhihu(cfg, log, limit):
    """知乎热榜 via a public RSSHub mirror.

    WHY a mirror: the first-party endpoints are login-walled —
    https://www.zhihu.com/api/v4/search/top_search answers 403
    {"error":{"need_login":true,...}} and https://www.zhihu.com/hot answers 403.
    We never log in; we read the public bridge instead.
    Verified 2026-10-01: https://rss.injahow.cn/zhihu/hotlist -> 200
    application/xml with 40 items.
    """
    last = None
    for base in RSSHUB_MIRRORS:
        try:
            rows = _collect_rss(f"{base}/zhihu/hotlist", "zhihu", cfg, log, limit, ["知乎", "热榜"])
        except Exception as e:
            last = e
            log(f"zhihu mirror {base} failed: {e}", "warn")
            continue
        if rows:
            if base != RSSHUB_MIRRORS[0]:
                log(f"zhihu 使用备用镜像 {base}", "warn")
            return rows
    if last:
        raise last
    return []


def collect_programmercarl(cfg, log, limit):
    """代码随想录 (programmercarl.com) —— 算法题解 / LeetCode Hot100 / 大模型 / 面经.

    WHY this source: the reader asked for algorithm material from dedicated sites rather
    than login-walled forums. This is the most widely used free algorithm curriculum in
    Chinese, and its robots.txt is explicit - `User-agent: * / Allow: /` plus a declared
    sitemap - so walking that sitemap is what the site asks crawlers to do.

    Sitemap composition (measured, 563 URLs): algo=298 (题解), hot100=48 (LeetCode Hot100),
    llm=26, kamacoder=49, daichong=31, qita=23, 前序=13. Depth is NOT recency-bound here:
    a 2021 KMP write-up is still the right answer today, which is why the reader wants
    past material organised rather than only the latest.
    """
    out: list[dict] = []
    try:
        _s, body, _ct = http_get("https://programmercarl.com/sitemap.xml",
                                 timeout=30, cfg=cfg, log=log, retries=1)
        sitemap = body.decode("utf-8", "replace")
    except Exception as e:
        log(f"programmercarl sitemap failed: {e}", "warn")
        return []

    urls = []
    for loc in re.findall(r"<loc>([^<]+)</loc>", sitemap):
        u = loc.strip()
        # Content pages only. Skip about/ and the training-camp pages: those are
        # commercial by definition and would only ever be dropped by the spam gate.
        if re.search(r"/(about|xunlian|ke)/", u):
            continue
        if not (u.endswith(".html") or u.endswith("/")):
            continue
        urls.append(u)
    if not urls:
        return []

    # Rotate so the library keeps growing instead of re-reading the same head daily.
    day_off = int(now_cst().strftime("%j")) * 5
    urls = urls[day_off % len(urls):] + urls[:day_off % len(urls)]

    scan_budget = max(limit * 3, limit + 8)
    scanned = 0
    for url in urls:
        if len(out) >= limit or scanned >= scan_budget:
            break
        scanned += 1
        try:
            _s, page_b, _ct = http_get(url, timeout=20, cfg=cfg, log=log, retries=1)
            page = page_b.decode("utf-8", "replace")
        except Exception as e:
            log(f"programmercarl page failed ({url}): {e}", "warn")
            continue

        title_m = re.search(r"<title>([^<]+)</title>", page)
        title = clean_text(title_m.group(1), 160) if title_m else ""
        title = re.sub(r"\s*[|｜]\s*代码随想录.*$", "", title).strip()
        if not title:
            continue

        content = re.sub(r"<script[^>]*>.*?</script>", " ", page, flags=re.S)
        content = re.sub(r"<style[^>]*>.*?</style>", " ", content, flags=re.S)
        m_body = re.search(
            r'<div[^>]*class="[^"]*(?:theme-default-content|content__default)[^"]*"[^>]*>(.*?)</div>\s*</div>',
            content, re.S)
        text = clean_text(re.sub(r"<[^>]+>", " ", m_body.group(1) if m_body else content), 1400)
        if len(text) < 120:
            continue

        lc = re.search(r"LeetCode\s*(\d+)", page, re.I)
        tags = ["算法", "代码随想录"]
        if "/hot100/" in url:
            tags.append("Hot100")
        if "/llm/" in url or "/qita/" in url:
            tags.append("大模型")
        if lc:
            tags.append(f"LC{lc.group(1)}")

        out.append({
            "sourceId": "programmercarl", "channel": "programmercarl",
            "externalId": url.rstrip("/").rsplit("/", 1)[-1] or url,
            "title": title,
            "summary": text,
            "url": url,
            "publishedAt": iso(now_cst()),
            "lang": "zh",
            "tags": tags,
            "codeAvailable": True,
        })
    log(f"programmercarl -> {len(out)} items", "info" if out else "warn")
    return out


def collect_kamacoder_notes(cfg, log, limit):
    """卡码笔记 (notes.kamacoder.com) —— 大模型/Java/C++ 八股 + 大厂面经.

    WHY: 八股/基础 was the thinnest category in the corpus (1.9%) while being exactly
    what the reader wants to accumulate. This site exists for that purpose - it is
    organised as llm/app, llm/transformer, interview/llm, interview/java, base/... and
    robots.txt is `Allow: /` with a declared sitemap (525 URLs, measured).
    """
    out: list[dict] = []
    try:
        _s, body, _ct = http_get("https://notes.kamacoder.com/sitemap.xml",
                                 timeout=30, cfg=cfg, log=log, retries=1)
        sitemap = body.decode("utf-8", "replace")
    except Exception as e:
        log(f"kamacoder notes sitemap failed: {e}", "warn")
        return []

    urls = []
    for loc in re.findall(r"<loc>([^<]+)</loc>", sitemap):
        u = loc.strip()
        # jianli/* is resume coaching (commercial) and _llm-master-sync is a mirror index.
        if "/jianli/" in u or "_llm-master-sync" in u or "/news" in u:
            continue
        if any(seg in u for seg in ("interview/", "/llm/", "/base/", "/java/", "/cpp/")):
            urls.append(u)
    if not urls:
        return []

    day_off = int(now_cst().strftime("%j")) * 3
    urls = urls[day_off % len(urls):] + urls[:day_off % len(urls)]

    scan_budget = max(limit * 3, limit + 8)
    scanned = 0
    for url in urls:
        if len(out) >= limit or scanned >= scan_budget:
            break
        scanned += 1
        try:
            _s, page_b, _ct = http_get(url, timeout=20, cfg=cfg, log=log, retries=1)
            page = page_b.decode("utf-8", "replace")
        except Exception as e:
            log(f"kamacoder notes page failed ({url}): {e}", "warn")
            continue

        title_m = re.search(r"<title>([^<]+)</title>", page)
        title = clean_text(title_m.group(1), 160) if title_m else ""
        title = re.sub(r"\s*[|｜]\s*卡码笔记.*$", "", title).strip()
        title = re.sub(r"^\d{4}最全", "", title).strip()
        if not title:
            continue

        content = re.sub(r"<script[^>]*>.*?</script>", " ", page, flags=re.S)
        content = re.sub(r"<style[^>]*>.*?</style>", " ", content, flags=re.S)
        text = clean_text(re.sub(r"<[^>]+>", " ", content), 1400)
        # Strip the repeated site chrome so the summary starts at the answer.
        for anchor in ("卡码笔记-最强八股文", "首页 计算机基础"):
            k = text.find(anchor)
            if k >= 0:
                text = text[k + len(anchor):].strip()
        if len(text) < 120:
            continue

        kind = "interview" if "interview/" in url else "fundamentals"
        tags = ["八股" if kind == "fundamentals" else "面经", "卡码笔记"]
        if "/llm/" in url:
            tags.append("大模型")
        out.append({
            "sourceId": "kamacoder_notes", "channel": "kamacoder_notes",
            "externalId": url.rstrip("/").rsplit("/", 1)[-1] or url,
            "title": title,
            "summary": text,
            "url": url,
            "publishedAt": iso(now_cst()),
            "lang": "zh",
            "tags": tags,
            "kind": kind,
        })
    log(f"kamacoder_notes -> {len(out)} items", "info" if out else "warn")
    return out


# Curated algorithm/interview repositories. A targeted list beats a search query here:
# these are the canonical high-star repos, their content is stable, and curating them
# lets each carry the right knowledge type instead of being guessed from a README.
GITHUB_ALGO_REPOS = [
    # 代码随想录作者的官方仓库（《代码随想录》200 题 60 万字题解），以及卡码网题解全集。
    # 这两者与 programmercarl / kamacoder 站点互补：站点给讲解，仓库给可检索的题解索引。
    ("youngyangyang04/leetcode-master", "coding", ["题解", "刷题攻略", "代码随想录"]),
    ("youngyangyang04/kamacoder-solutions", "coding", ["题解", "卡码网"]),
    ("doocs/leetcode", "coding", ["题解", "多语言", "LeetCode"]),
    ("azl397985856/leetcode", "coding", ["题解", "LeetCode", "算法"]),
    ("halfrost/LeetCode-Go", "coding", ["题解", "Go", "LeetCode"]),
    ("haoel/leetcode", "coding", ["题解", "LeetCode"]),
    # 牛客题霸的题解镜像：不需要登录牛客即可获得题目与解法。
    ("waylau/nowcoder-exam-oj", "coding", ["题解", "牛客题霸"]),
    ("huihut/interview", "fundamentals", ["八股", "C++", "计算机基础"]),
    ("InterviewMap/CS-Interview-Knowledge-Map", "fundamentals", ["八股", "面试知识图谱"]),
    ("donnemartin/interactive-coding-challenges", "coding", ["编程题", "面试题"]),
    ("liyupi/mianshiya", "fundamentals", ["面试题库", "八股"]),
    ("csguide-dabai/interview-guide", "fundamentals", ["八股", "后端"]),
    ("ashishps1/awesome-leetcode-resources", "coding", ["LeetCode", "学习资源"]),
    ("kunal-kushwaha/DSA-Bootcamp-Java", "coding", ["数据结构", "算法"]),
    ("Blankj/awesome-java-leetcode", "coding", ["题解", "Java"]),
]


def collect_github_algo(cfg, log, limit):
    """GitHub 上的算法题解 / 面试知识仓库（含"过往"内容 —— 题解不过期）.

    WHY repos are their own knowledge type here: the generic `github` channel returns
    trending projects, which are `project` material (a framework to learn) and essentially
    never algorithm problems. The reader explicitly asked for leetcode/洛谷/OJ repos.

    These are DELIBERATELY not recency-filtered. A 2019 LeetCode solution repo is still
    correct today, and the reader asked for past material to be organised too - so the bar
    is stars plus a real description, not publish date.
    """
    out: list[dict] = []
    token = os.environ.get("GITHUB_TOKEN") or ""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for full, kind, tags in GITHUB_ALGO_REPOS:
        if len(out) >= limit:
            break
        # Repo metadata changes slowly, so an unchanged repo should not be re-fetched
        # every day; cache for 3 days.
        ck = DATA_DIR / "cache" / f"ghalgo-{full.replace('/', '_')}.json"
        data = None
        try:
            if ck.exists() and (time.time() - ck.stat().st_mtime) < 86400 * 3:
                data = json.loads(ck.read_text("utf-8"))
        except Exception:
            data = None
        if data is None:
            try:
                _s, raw, _ct = http_get(f"https://api.github.com/repos/{full}",
                                        timeout=20, cfg=cfg, log=log, retries=1,
                                        headers=headers)
                data = json.loads(raw.decode("utf-8", "replace"))
                ck.parent.mkdir(parents=True, exist_ok=True)
                ck.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
            except Exception as e:
                log(f"github_algo {full} failed: {e}", "warn")
                continue

        if not isinstance(data, dict) or data.get("message"):
            continue
        stars = int(data.get("stargazers_count") or 0)
        if stars < 300:
            continue                        # a fork or a dead repo: not worth a card
        desc = clean_text(data.get("description") or "", 300)
        topics = [str(t) for t in (data.get("topics") or [])][:8]

        # The summary is composed ONLY from observed facts - never an invented blurb.
        bits = []
        if desc:
            bits.append(desc)
        bits.append(f"{stars} stars")
        if data.get("language"):
            bits.append(str(data["language"]))
        if data.get("pushed_at"):
            bits.append(f"最近更新 {str(data['pushed_at'])[:10]}")
        if topics:
            bits.append("主题：" + "、".join(topics))
        summary = "｜".join(bits)
        if len(summary) < 40:
            continue

        out.append({
            "sourceId": "github_algo", "channel": "github_algo",
            "externalId": full,
            "title": f"{full} — {desc[:70]}" if desc else full,
            "summary": summary,
            "url": data.get("html_url") or f"https://github.com/{full}",
            "publishedAt": data.get("pushed_at") or iso(now_cst()),
            "lang": "en" if not re.search(r"[\u4e00-\u9fff]", desc or "") else "zh",
            "tags": ["GitHub", "算法仓库"] + tags,
            # NOTE: `kind` is deliberately NOT set from the curated table. `kind` is
            # authoritative and would force huihut/interview ("技术面试基础知识总结")
            # into `job`; it is 八股 material. Letting classify_knowledge_type() judge
            # from the description keeps each repo in the right bucket.
            "stars": stars,
            "codeAvailable": True,
            "qualitySignals": {"stars": stars},
        })
    log(f"github_algo -> {len(out)} items", "info" if out else "warn")
    return out


def collect_nowcoder_questions(cfg, log, limit):
    """牛客试题广场的编程题/算法题（题目陈述 + 时间/空间限制）。

    Source of truth is the sitemap the site DECLARES in robots.txt
    (`sitemap/question/sitemap*.xml`), so this reads exactly what 牛客 asks crawlers
    to index. Each `questionTerminal` page carries a real problem statement:

        [编程题]小月的亮灯
        热度指数：7  时间限制：C/C++ 2秒，其他语言4秒  空间限制：C/C++ 256M
        小月在一条刻度线上布置了 盏灯，状态用…

    That is precisely the 算法题 material the workbench was missing. Parsing is
    deliberately conservative: only a `[编程题]`/`[问答题]` marker plus the statement
    body is accepted, so a page-template change yields zero items rather than garbage.
    """
    out = []
    # Bound the work: the sitemap index points at shards, and the site also publishes
    # flat lists per subject. `questionurl1.txt` is 2.5 MB / 45k lines holding BOTH
    # /practice/ programming problems (2,970 of them) and /discuss/ threads (42,030),
    # so it is cached for a week rather than re-downloaded every run.
    page_urls: list[str] = []
    cache = DATA_DIR / "cache" / "nowcoder-question-list.txt"
    fresh_enough = False
    try:
        if cache.exists():
            age_h = (time.time() - cache.stat().st_mtime) / 3600.0
            fresh_enough = age_h < 24 * 7
    except OSError:
        fresh_enough = False
    if not fresh_enough:
        try:
            _s, body, _ct = http_get(
                "https://www.nowcoder.com/sitemap/question/questionurl1.txt",
                timeout=40, cfg=cfg, log=log, retries=1)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(body)
            log("nowcoder_questions: refreshed the published question list", "info")
        except Exception as e:
            log(f"nowcoder_questions list refresh failed ({e}); using cache if present",
                "warn")
    if cache.exists():
        try:
            # Only programming problems: an objective question is 八股 and is handled by
            # the sitemap shard below, while /discuss/ belongs to the interview channel.
            for line in cache.read_text("utf-8", "replace").splitlines():
                u = line.strip().split("?")[0]
                if "/practice/" in u:
                    page_urls.append(u)
        except Exception as e:
            log(f"nowcoder_questions cache read failed: {e}", "warn")

    # The sitemap shard adds 客观题 (八股) which the practice list does not contain.
    shard_urls = [
        "https://www.nowcoder.com/sitemap/question/sitemap1.xml",
        "https://www.nowcoder.com/sitemap/question/sitemap0212.xml",
    ]
    for sm in shard_urls:
        try:
            _s, body, _ct = http_get(sm, timeout=20, cfg=cfg, log=log, retries=1)
            text = body.decode("utf-8", "replace")
        except Exception as e:
            log(f"nowcoder_questions sitemap failed ({sm}): {e}", "warn")
            continue
        for loc in re.findall(r"<loc>([^<]+)</loc>", text):
            clean = loc.split("?")[0].strip()
            if "questionTerminal" in clean:
                page_urls.append(clean)

    # The sitemap shard is stable, so rotate through it rather than always taking the
    # same head: a rotating offset means the library keeps growing day over day.
    #
    # The published list is PROGRESSIVE (it opens with beginner warm-ups like 判断字母
    # and 及格分数), and a beginner drill is not what an aspiring algorithm-intern
    # candidate needs. Skipping the first tenth of each list biases the sample toward
    # the substantive end while still rotating, so coverage keeps advancing.
    if page_urls:
        skip = len(page_urls) // 10
        page_urls = page_urls[skip:]
        day_off = int(now_cst().strftime("%j")) * 7
        page_urls = page_urls[day_off % len(page_urls):] + page_urls[:day_off % len(page_urls)]

    seen = set()
    # Fetching a candidate page is not enough to keep it: the content filter rejects
    # hardware exercises and anything with no algorithm/ML signal, so the crawler must
    # look at more candidates than the number of items it wants. 6x is the measured
    # ratio needed to fill a batch from an arbitrary slice of the problem set.
    scan_budget = max(limit * 6, limit + 12)
    scanned = 0
    for url in page_urls:
        if len(out) >= limit or scanned >= scan_budget:
            break
        scanned += 1
        if url in seen:
            continue
        seen.add(url)
        try:
            _s, body, _ct = http_get(url, timeout=20, cfg=cfg, log=log, retries=1)
            page = body.decode("utf-8", "replace")
        except Exception as e:
            log(f"nowcoder_questions page failed: {e}", "warn")
            continue

        title_m = re.search(r"<title>([^<]+)</title>", page)
        title = clean_text(title_m.group(1), 160) if title_m else ""
        # Strip the site suffix the page title carries ("…_牛客题霸_牛客网").
        title = re.sub(r"_+牛客(题霸|网)?_*牛客网\s*$", "", title).strip(" _-")
        title = re.sub(r"_+牛客题霸\s*$", "", title).strip(" _-")
        marker = re.search(r"\[(编程题|问答题|单选题|多选题)\]\s*([^<\n]{2,120})", page)
        if marker:
            kind_zh = marker.group(1)
            if not title:
                title = clean_text(marker.group(2), 160)
        else:
            kind_zh = ""
        if not title:
            continue

        # Statement body: the visible text right after the metadata block.
        body_text = re.sub(r"<script[^>]*>.*?</script>", " ", page, flags=re.S)
        body_text = re.sub(r"<style[^>]*>.*?</style>", " ", body_text, flags=re.S)
        body_text = clean_text(re.sub(r"<[^>]+>", " ", body_text), 1200)
        # Trim the site chrome so the summary starts at the actual problem.
        cut = body_text.find("空间限制")
        if cut > 0:
            body_text = body_text[cut:]
            body_text = re.sub(r"^空间限制[^ ]*[^。]*?[，。:：]\s*", "", body_text)
        statement = clean_text(body_text, 600)
        if len(statement) < 40:
            continue                      # template change: refuse to invent content

        # Relevance filter: skip explicit hardware-design exercises and anything with
        # no algorithm/ML signal at all. Position-based sampling cannot do this (the
        # problem set is ordered by category, so a slice can be entirely FPGA).
        hay = f"{title} {statement}"
        if PROBLEM_EXCLUDE_RE.search(hay):
            continue
        if not PROBLEM_RELEVANT_RE.search(hay):
            continue

        limits = ""
        lim_m = re.search(r"时间限制[：:]\s*([^ 空]{2,40})", page)
        if lim_m:
            limits = clean_text(lim_m.group(1), 40)
        hot_m = re.search(r"热度指数[：:]\s*(\d+)", page)

        out.append({
            "sourceId": "nowcoder_questions", "channel": "nowcoder_questions",
            "externalId": url.rsplit("/", 1)[-1],
            "title": f"[{kind_zh or '编程题'}] {title}",
            "summary": statement,
            "url": url,
            "publishedAt": iso(now_cst()),
            "lang": "zh",
            "tags": ["算法题", "牛客", kind_zh or "编程题"],
            # NOTE: deliberately NOT setting `kind`. `kind` is authoritative and would
            # force every item to one knowledge type, but this channel yields BOTH
            # 编程题 (algorithm problems) and 单选题 (八股: probability, ML metrics,
            # inference engineering). Letting classify_knowledge_type() judge from the
            # title's [编程题]/[单选题] marker puts each in the right bucket - the
            # reader wants those tracked separately.
            "qualitySignals": {
                "hotIndex": int(hot_m.group(1)) if hot_m else 0,
                "timeLimit": limits,
            },
            "codeAvailable": True,
        })
    log(f"nowcoder_questions -> {len(out)} items", "info" if out else "warn")
    return out


def collect_nowcoder(cfg, log, limit):
    """Nowcoder public discussion pages.

    WHY the regex changed: the listing is server-rendered, but each title sits
    inside nested <span>s —
      <a href="/discuss/933114179207069696?sourceSSR=home" ... class="po">
        <span ...><span ...>机械工程相关题目</span></span></a>
    so the old `href="(/discuss/\\d+...)"[^>]*>\\s*([^<]{8,120})<` matched 0 of 20
    links while the page itself was a healthy 200. Verified 2026-10-01: the
    tag-tolerant pattern below yields 10 (href, title) pairs including 面经/实习
    threads. robots.txt disallows /search, /nccommon and /ab/ab-test-flow only.
    """
    out = []
    link_re = re.compile(r'<a[^>]*href="(/discuss/\d+)[^"]*"[^>]*>(.*?)</a>', re.S)
    for url, kind in [("https://www.nowcoder.com/discuss?type=0&order=0", "discuss"),
                      ("https://www.nowcoder.com/discuss?type=2&order=0", "job")]:
        try:
            _s, body, _ct = http_get(url, timeout=20, cfg=cfg, log=log, retries=1)
            text = body.decode("utf-8", "replace")
        except Exception as e:
            log(f"nowcoder {kind} failed: {e}", "warn")
            continue
        found, seen = 0, set()
        for m in link_re.finditer(text):
            href, inner = m.group(1), m.group(2)
            title = clean_text(re.sub(r"<[^>]+>", " ", inner), 200)
            if not title or len(title) < 6 or href in seen:
                continue
            seen.add(href)
            out.append({
                "sourceId": "nowcoder", "channel": "nowcoder", "externalId": href,
                "title": title,
                "summary": "牛客社区公开讨论页，可能包含面经或招聘信息。",
                "url": "https://www.nowcoder.com" + href,
                "publishedAt": iso(now_cst()),
                "tags": ["牛客", "面经"], "lang": "zh",
            })
            found += 1
            if found >= limit:
                break
        log.detail(f"nowcoder {kind} -> {found}")
        time.sleep(0.8)
    return out


def _jobs_common(source_id, rows, cfg):
    """Normalize heterogeneous job payloads into one item shape.

    WHY the key lists are long: Tencent's API returns RecruitPostName / PostURL /
    LocationName / LastUpdateTime, while ByteDance-style payloads use
    title / url / city / publishTime. Looking up a single field name is exactly
    what made a 200-OK, 339-opening Tencent feed report as `empty`.
    """
    out = []
    for r in rows if isinstance(rows, list) else []:
        title = clean_text(first_of(r, ("title", "name", "jobName", "RecruitPostName",
                                        "positionName", "postName", "jobTitle")), 200)
        if not title or not re.search(r"算法|模型|多模态|大模型|AI|machine learning|algorithm|research",
                                      title, re.I):
            continue
        desc_keys = ("description", "requirement", "Responsibility", "jobDescription",
                     "descriptionHtml", "city", "cityName", "workCity", "LocationName",
                     "workLocation", "category", "jobCategory", "CategoryName",
                     "ProductName", "BGName")
        desc_bits = [clean_text(r.get(k), 240) for k in desc_keys if r.get(k)]
        url = first_of(r, ("url", "jobUrl", "postUrl", "PostURL", "detailUrl",
                           "positionUrl", "redirectUrl", "submitUrl"))
        out.append({
            "sourceId": source_id, "channel": source_id,
            "externalId": str(first_of(r, ("id", "code", "RecruitPostId", "PostId",
                                           "positionId", "jobId")) or title_key(title)),
            "title": title,
            "summary": clean_text(" | ".join(b for b in desc_bits if b), 600),
            "url": url,
            "publishedAt": iso(parse_ts(first_of(r, ("publishTime", "updateTime", "releaseTime",
                                                     "LastUpdateTime", "publishDate"))) or now_cst()),
            "tags": ["招聘"] + [t for t in (clean_text(first_of(r, ("city", "cityName",
                                                                   "workCity", "LocationName")), 40),) if t],
            "lang": "zh",
        })
    return out


def collect_jobs_bytedance(cfg, log, limit):
    url = ("https://jobs.bytedance.com/api/v1/search/job/posts?keyword=" +
           urllib.parse.quote("算法实习") + "&limit=20&offset=0&job_category_id_list=&tag_id_list="
           "&location_code_list=&subject_id_list=&recruitment_id_list=1&portal_type=6")
    data = http_json(url, cfg=cfg, log=log, retries=1, timeout=30,
                     headers={"Referer": "https://jobs.bytedance.com/experienced/position"})
    rows = ((data or {}).get("data") or {}).get("job_post_list") or []
    return _jobs_common("jobs_bytedance", rows, cfg)


def collect_jobs_tencent(cfg, log, limit):
    """腾讯招聘 — worked all along; the bug was on our side.

    Verified 2026-10-01: HTTP 200 JSON,
    {"Code":200,"Data":{"Count":339,"Posts":[... 20 rows ...]}}, each row
    carrying RecruitPostName / PostURL / LastUpdateTime / LocationName /
    Responsibility. `pageSize=10` and `pageSize=20` both answer at once, so the
    channel previously reported `empty` only because _jobs_common looked up
    title/name/jobName and never saw RecruitPostName.
    """
    url = ("https://careers.tencent.com/tencentcareer/api/post/Query?timestamp=0&countryId=&cityId="
           "&bgId=&bgIds=&productId=&categoryId=&parentCategoryId=&attrId=&keyword=" +
           urllib.parse.quote("算法") + "&pageIndex=1&pageSize=%d&language=zh-cn&area=cn"
           % min(max(20, limit), 50))
    data = http_json(url, cfg=cfg, log=log, retries=1, timeout=30)
    rows = ((data or {}).get("Data") or {}).get("Posts") or []
    return _jobs_common("jobs_tencent", rows, cfg)


def collect_jobs_alibaba(cfg, log, limit):
    """阿里招聘 — DISABLED (see DISABLED_CHANNELS); endpoint kept for reference.

    The old /api/job/search path is a 404 Whitelabel page. The API the live SPA
    actually calls is POST https://talent.alibaba.com/position/search (its JS
    bundle maps `/position/search` and `/searchCondition/list`), but GET answers
    405, POST without a CSRF token answers 403, and POST with the CSRF token the
    public page itself sets answers 200 `{"success":true,"datas":null,
    "totalCount":0}` for every payload shape tried. The backend wants a token
    only Alibaba's baxia browser script can mint, so this stays blocked rather
    than turning into a redirect-chasing exercise.
    """
    url = ("https://talent.alibaba.com/api/job/search?keyword=" + urllib.parse.quote("算法") +
           "&pageSize=20&pageNo=1&language=zh")
    data = http_json(url, cfg=cfg, log=log, retries=1, timeout=30)
    rows = data.get("data") or (data.get("content") or {}).get("datas") or []
    if isinstance(rows, dict):
        rows = rows.get("list") or []
    return _jobs_common("jobs_alibaba", rows, cfg)


def _generic_jobs_html(source_id, url, cfg, log, limit):
    """For career pages without a public API: extract only visible job links."""
    out = []
    _s, body, _ct = http_get(url, timeout=25, cfg=cfg, log=log, retries=1)
    text = body.decode("utf-8", "replace")
    seen = set()
    for m in re.finditer(r'href="([^"]{4,200})"[^>]*>([^<]{6,90})<', text):
        href, label = m.group(1), clean_text(m.group(2), 90)
        if not label or label in seen:
            continue
        if not re.search(r"算法|模型|实习|多模态|大模型|AI|研究", label):
            continue
        seen.add(label)
        full = href if href.startswith("http") else urllib.parse.urljoin(url, href)
        out.append({
            "sourceId": source_id, "channel": source_id, "externalId": full,
            "title": label, "summary": f"{CHANNEL_NAMES.get(source_id, source_id)} 招聘页公开条目",
            "url": full, "publishedAt": iso(now_cst()), "tags": ["招聘"], "lang": "zh",
        })
        if len(out) >= limit:
            break
    return out


def collect_jobs_zhipu(cfg, log, limit):
    """智谱招聘 — DISABLED (see DISABLED_CHANNELS), code kept for reference.

    zhipuai.cn/joinus answers 200 with 783KB of Next.js RSC payload, but it is a
    marketing page: 算法/校招 appear only as i18n category labels, there is no
    per-job data, no ATS host (only generic feishu.cn form links) and none of the
    15 referenced _next chunks calls a job API.
    """
    return _generic_jobs_html("jobs_zhipu", "https://zhipuai.cn/joinus", cfg, log, limit)


def collect_jobs_moonshot(cfg, log, limit):
    """月之暗面 — DISABLED (see DISABLED_CHANNELS), code kept for reference.

    www.moonshot.cn/careers answers 200 with 95KB and zero job keywords, and now
    delegates to careers.kimi.com, which is an 11.6KB JS shell with only three
    RSC pushes, no embedded data (/jobs and /positions are 404) and no API path
    in its chunks.
    """
    return _generic_jobs_html("jobs_moonshot", "https://www.moonshot.cn/careers", cfg, log, limit)


def collect_jobs_deepseek(cfg, log, limit):
    """DeepSeek 招聘 — 备用来源 is the Moka ATS snapshot baked into the site bundle.

    WHY this shape: https://www.deepseek.com/careers is a hard 404 (the homepage
    links to https://talent.deepseek.com/ instead). That host is a 588-byte SPA
    shell whose own API needs a browser, but its production bundle
    /static/main.<hash>.js ships a build-time snapshot of the Moka listing as a
    literal `JSON.parse('{"crawledAt":...,"sourceUrl":"https://app.mokahr.com/
    social-recruitment/high-flyer/140576","total":N,"jobs":[...]}')` — the exact
    data the page renders from. Verified 2026-10-01: 34 jobs, including
    预训练/后训练/多模态理解 研究员 and 大模型训练/推理框架工程师.
    """
    _s, body, _ct = http_get("https://talent.deepseek.com/", timeout=25, cfg=cfg, log=log, retries=1)
    page = body.decode("utf-8", "replace")
    m = (re.search(r'<script[^>]+src="(/static/main[^"]+\.js)"', page)
         or re.search(r'"(/static/main\.[0-9a-z]+\.js)"', page))
    if not m:
        log("deepseek: main bundle not referenced by the landing page", "warn")
        return []
    _s, js_body, _ct = http_get("https://talent.deepseek.com" + m.group(1),
                                timeout=30, cfg=cfg, log=log, retries=1)
    js = js_body.decode("utf-8", "replace")
    lit = re.search(r"JSON\.parse\('(\{\"crawledAt\".*?)'\)", js, re.S)
    if not lit:
        log("deepseek: embedded job catalogue literal not found", "warn")
        return []
    try:
        data = json.loads(js_literal_decode(lit.group(1)))
    except Exception as e:
        raise ValueError(f"deepseek catalogue literal unreadable: {e}") from e
    out = []
    for job in (data.get("jobs") or [])[:max(1, int(limit or 1))]:
        title = clean_text(job.get("title") or "", 200)
        if not title:
            continue
        detail = job.get("detailUrl") or data.get("sourceUrl") or "https://talent.deepseek.com/"
        out.append({
            "sourceId": "jobs_deepseek", "channel": "jobs_deepseek",
            "externalId": str(job.get("id") or title_key(title)),
            "title": title,
            "summary": clean_text(" | ".join(
                x for x in [job.get("functionName"),
                            "、".join(job.get("locations") or []),
                            clean_text(job.get("descriptionHtml") or "", 400)] if x), 600),
            "url": detail,
            "publishedAt": iso(parse_ts(data.get("crawledAt")) or now_cst()),
            "tags": ["招聘", "DeepSeek"] + [t for t in (job.get("functionName"),) if t],
            "qualitySignals": {"via": "moka-ats-snapshot", "crawledAt": data.get("crawledAt"),
                               "sourceUrl": data.get("sourceUrl")},
            "lang": "zh",
        })
    if not out:
        log("deepseek: catalogue contained no jobs", "warn")
    return out


def collect_jobs_minimax(cfg, log, limit):
    """MiniMax — DISABLED (see DISABLED_CHANNELS), code kept for reference.

    minimaxi.com/careers renders an embedded Feishu ATSX portal
    (vrfi1sk8a0.jobs.feishu.cn, robots.txt disallows /api/). The portal index is
    a 126KB shell with no job rows and its API answers 405 exactly like
    ByteDance's, so there is nothing to parse without a browser.
    """
    return _generic_jobs_html("jobs_minimax", "https://www.minimaxi.com/careers", cfg, log, limit)


def collect_jobs_shailab(cfg, log, limit):
    """上海 AI Lab — DISABLED (see DISABLED_CHANNELS), code kept for reference.

    /joinus, /joinus/social and /joinus/campus all return the same 70KB
    marketing page with no job rows; openings arrive over JS and no ATS/API
    endpoint is referenced anywhere in the page or its scripts.
    """
    return _generic_jobs_html("jobs_shailab", "https://www.shlab.org.cn/joinus", cfg, log, limit)


def collect_inbox(cfg, log, limit):
    """Human-in-the-loop import.

    Reads only local files produced by scripts/inbox.py from web/data/inbox/.
    This makes login-walled channels (Xiaohongshu, BOSS Zhipin, Lagou, Shixiseng)
    usable WITHOUT ever making a network request to them - which is both the
    legally correct behaviour and the only stable one.

    Re-reads every run so edits to the source rows are picked up; the deduper
    collapses repeats, so re-importing is free.
    """
    out = []
    inbox = DATA_DIR / "inbox"
    if not inbox.exists():
        return out
    for path in sorted(inbox.glob("normalized-*.json")):
        try:
            payload = json.loads(path.read_text("utf-8"))
        except Exception as e:
            log(f"inbox: {path.name} unreadable ({e})", "warn")
            continue
        rows = payload.get("items") if isinstance(payload, dict) else payload
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            raw = dict(row)
            raw["sourceId"] = "inbox"
            raw["channel"] = "inbox"
            out.append(raw)
    if out:
        log.detail(f"inbox -> {len(out)} rows from {inbox.name}/*.json")
    return out


COLLECTORS = {
    "arxiv": collect_arxiv, "hf_papers": collect_hf_papers, "hf_models": collect_hf_models,
    "github": collect_github, "gh_trending": collect_gh_trending,
    "openreview": collect_openreview, "s2": collect_s2, "hn": collect_hn, "reddit": collect_reddit,
    "machineheart": collect_machineheart, "qbitai": collect_qbitai, "leiphone": collect_leiphone,
    "hf_blog_rss": collect_hf_blog_rss, "openai_rss": collect_openai_rss,
    "rsshub": collect_rsshub, "zhihu": collect_zhihu, "nowcoder": collect_nowcoder,
    "nowcoder_questions": collect_nowcoder_questions,
    "programmercarl": collect_programmercarl,
    "kamacoder_notes": collect_kamacoder_notes,
    "github_algo": collect_github_algo,
    "jobs_bytedance": collect_jobs_bytedance, "jobs_tencent": collect_jobs_tencent,
    "jobs_alibaba": collect_jobs_alibaba, "jobs_zhipu": collect_jobs_zhipu,
    "jobs_moonshot": collect_jobs_moonshot, "jobs_deepseek": collect_jobs_deepseek,
    "jobs_minimax": collect_jobs_minimax, "jobs_shailab": collect_jobs_shailab,
    "inbox": collect_inbox,
}


# ============================================================================
# 5. CONFIG
# ============================================================================
def deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path, log: Logger) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))          # deep copy
    cats = json.loads(json.dumps(DEFAULT_CATEGORIES))
    if path.exists():
        try:
            user = json.loads(path.read_text("utf-8"))
            user_cats = user.pop("categories", {}) or {}
            for cid, c in user_cats.items():
                if cid in cats:
                    merged = dict(cats[cid])
                    merged.update({k: v for k, v in c.items() if k != "keywords"})
                    extra = [k for k in (c.get("keywords") or []) if k not in merged.get("keywords", [])]
                    merged["keywords"] = list(merged.get("keywords", [])) + extra
                    # keywordRemove lets the self-tuning pass DELETE a built-in
                    # keyword. The list above can only ever append, so without an
                    # explicit removal channel a keyword proven to be too broad
                    # ("review", "theory") could never be retired.
                    rm = {str(k).lower() for k in (c.get("keywordRemove") or [])}
                    if rm:
                        merged["keywords"] = [k for k in merged["keywords"] if str(k).lower() not in rm]
                    cats[cid] = merged
                else:
                    cats[cid] = c
            cfg = deep_merge(cfg, user)
            log(f"config loaded: {path}")
        except Exception as e:
            log(f"config parse failed, using defaults: {e}", "warn")
    else:
        log(f"config not found at {path}, using built-in defaults", "warn")
    cfg["categories"] = cats
    for c in cfg["categories"].values():
        c["keywords"] = [str(k).lower() for k in (c.get("keywords") or [])]
    return cfg


def save_default_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), "utf-8")


# ============================================================================
# 6. CLASSIFY + SCORE
# ============================================================================
def _kw_match(kw: str, text: str) -> bool:
    """Does `kw` appear in `text` as a real token rather than a substring?

    WHY THIS MATTERS (a real classification defect, found by the deep-read agent):
    the classifier used plain substring containment, so short English keywords
    matched unrelated words inside longer ones and dragged nonsense into the corpus:

        'dit'    matched  audit, traditional, conditions
        'intern' matched  international, internal
        'ppo'    matched  Opportunities
        'review' matched  reviewed, preview
        '基础'   matched  BYD/Guoxuan battery-industry news (a real substring, but
                          irrelevant to the 'foundation' ML category)

    Evidence from one run: an education-assessment paper and a psychiatry survey
    were scored into 'generative', a UN speech into 'job', a law white-paper ranked
    FIRST in 'rl'. Cleaning that up by hand was a daily chore.

    Rule: keywords containing only ASCII word characters are matched on word
    boundaries. Keywords containing CJK (or punctuation) keep substring matching,
    because Chinese has no word boundaries and '多模态' must match inside
    '多模态大模型'. Short ASCII keywords (<4 chars) also require a boundary, which
    is exactly the 'dit'/'ppo' case.
    """
    if not kw:
        return False
    if not kw.isascii():
        return kw in text
    # Escape the keyword, then require non-word characters (or the string edges).
    return re.search(r"(?<![0-9a-z])" + re.escape(kw) + r"(?![0-9a-z])", text) is not None


def classify(item: dict, cfg: dict):
    blob = " ".join([str(item.get("title") or ""), str(item.get("summary") or ""),
                     " ".join(item.get("tags") or []), str(item.get("venue") or "")]).lower()
    title_blob = str(item.get("title") or "").lower()
    interests = cfg.get("interests") or {}
    scores, hits = {}, {}
    for cid, c in cfg["categories"].items():
        w = float(c.get("weight", 0.8)) * float(interests.get(cid, 0.8))
        sc, h = 0.0, []
        for kw in c.get("keywords") or []:
            if not kw:
                continue
            if _kw_match(kw, title_blob):
                sc += 3.0
                h.append(kw)
            elif _kw_match(kw, blob):
                sc += 1.0
                h.append(kw)
        for ac in (c.get("arxiv") or []):
            if ac and ac in (item.get("tags") or []):
                sc += 1.5
        if sc > 0:
            scores[cid] = sc * w
            hits[cid] = h
    if not scores:
        # Zero keyword hits. Previously this silently became "trend", which is how
        # a grammar/pragmatics paper with an EMPTY categoryHits ended up in the
        # trend queue. A weak match on nothing is not a trend - it is unclassified.
        return "unclassified", []
    best = max(scores.items(), key=lambda kv: (kv[1], kv[0]))
    return best[0], (hits.get(best[0]) or [])[:12]


# ============================================================================
# 6b. KNOWLEDGE TYPE - what KIND of material is this?
# ============================================================================
#
# WHY THIS EXISTS (the reader's complaint, restated as an engineering problem):
#   "很多资讯和采集的论文等是一看最新或关联就放进来了"
#
#   The old ranking gave a category match at most 40 points, freshness alone 30, and
#   the source tier a flat 12 - so a single generic keyword hit plus recency was
#   enough to clear the bar. Measured on the 664-item corpus: **55% of items entered
#   on exactly ONE keyword**, and only 7.4% of the corpus was actual mechanism-level
#   technical material while 14.2% was interview chatter. In other words the score
#   measured "is this recent and roughly on-topic", not "is this worth accumulating".
#
#   The workbench is supposed to ACCUMULATE: 核心/关键/扩展知识、算法深度解析、
#   岗位与面经、八股、领域算法知识点. So scoring now asks two independent questions:
#     1. how strong is the category evidence (specificity x count, not a flat +8 each)
#     2. what TYPE of material is this (depth is worth more than news, by design)
#
#   Order matters. `method` is tested before `news` because a release note WITH a
#   mechanism explanation should count as depth; `news` is tested before `interview`
#   so an acquisition story is not mistaken for a write-up just because it says
#   "engineer".
KNOWLEDGE_TYPE_RULES: list[tuple[str, "re.Pattern"]] = [
    # 核心/关键/扩展知识 · 算法深度解析
    ("method", re.compile(
        r"机制|原理|推导|架构|设计选择|为什么|深入|剖析|重新思考|解构|"
        r"we propose|we present|we introduce|we show|our method|novel\s+\w*\s*method|"
        r"mechanism|principle|derivation|architecture|rethink|revisit|"
        r"analysis of|understanding|unified framework|theorem|proof", re.I)),
    # 算法题 · 手撕
    ("coding", re.compile(
        r"手撕|手写|算法题|题解|leetcode|刷题|动态规划|双指针|并查集|单调栈|回溯|贪心|"
        r"binary search|two pointers|sliding window|union find|backtracking|"
        r"coding (?:problem|interview|question)", re.I)),
    # 面经 · 面试
    ("interview", re.compile(
        r"面经|面试|一面|二面|三面|hr面|挂经|凉经|oc\b|offer|offer选择|"
        r"interview (?:experience|questions?|process)|onsite", re.I)),
    # 八股 · 基础 · 领域知识点
    ("fundamentals", re.compile(
        r"八股|必考|高频考点|常见问题|基础|入门|教程|速查|cheat ?sheet|"
        r"transformer|attention|batchnorm|\bbn\b|dropout|softmax|交叉熵|反向传播|"
        r"梯度|优化器|adam|损失函数|kv.?cache|quantization|归一化", re.I)),
    # 岗位 · 招聘
    ("job", re.compile(
        r"实习|招聘|岗位|校招|社招|内推|jd\b|job description|"
        r"we are hiring|engineer \(m/f/d\)|internship|research intern", re.I)),
    # 纯资讯/融资/发布公告（不积累价值）
    ("news", re.compile(
        r"raises \$|raised \$|funding round|series [a-e]\b|acquires|acquisition|"
        r"\bipo\b|earnings|valued at|partnership|"
        r"announces|introducing|launch(?:es|ed)? (?:new|the)|press release|"
        r"reimagining|expands? (?:to|into)|now available|generally available", re.I)),
    # 观点/求职吐槽/泛化提问（低信息量）
    ("opinion", re.compile(
        r"吐槽|求建议|求助|怎么选|该不该|有没有人|想问一下|想请大家|"
        r"rant|opinion|thoughts on|should i|advice", re.I)),
]

# Keywords that are the CATEGORY NAME rather than a discriminating term. They are
# legitimate for classification (a paper about multimodal models IS multimodal) but
# they are weak EVIDENCE of value, so a single such hit is not enough on its own.
GENERIC_KEYWORDS = {
    "multimodal", "multi-modal", "vlm", "mllm", "lvlm", "vision-language",
    "multimodal llm", "agent", "agentic", "llm", "large language model",
    "post-training", "post training", "training", "model", "benchmark",
    "reinforcement learning", "diffusion", "world model", "reasoning",
    "foundation model", "generative", "inference", "memory", "alignment",
}

# Title-level signals that decide the knowledge type outright. Titles are short and
# deliberate, so a 面经/挂经 in the title is far stronger evidence than any phrase that
# happens to appear inside a long abstract.
EXPLICIT_INTERVIEW = re.compile(
    r"面经|挂经|凉经|笔经|面試|面试题|面试经验|"
    r"(?:^|[\s【\[（(])(一面|二面|三面|四面|hr面|1面|2面|3面|初面|终面)", re.I)
EXPLICIT_CODING = re.compile(
    r"手撕|手写代码|算法题|题解|刷题|leetcode|力扣|"
    r"\[编程题\]|"
    r"binary search|two pointers|sliding window|union find|backtracking", re.I)
# 八股/基础 markers. A 牛客 单选题 about probability, ML metrics or inference engineering,
# a C++ 语言特性 explanation, and a 计算机基础 article are all fundamentals material -
# not algorithm problems, not 面经. These are tested explicitly and BEFORE both the
# interview and the coding patterns, because:
#   · "[单选题] …以下哪项…" must not be read as an algorithm problem;
#   · "技术面试基础知识总结" is 八股, even though it contains the word 面试
#     (measured: huihut/interview landed in `interview` before this ordering).
EXPLICIT_FUNDAMENTALS = re.compile(
    r"\[单选题\]|\[多选题\]|\[问答题\]|八股|必考|高频考点|"
    r"以下哪(?:个|项)|下列说法|关于.{0,12}的说法|"
    # 题库/汇总 collections are reference material, not one person's interview story.
    # Measured: "liyupi/mianshiya — 企业面试题库网站" was being read as `interview`.
    r"面试题库|题库|汇总|知识图谱|知识总结|总结大全|"
    # 计算机基础 / 语言基础 八股
    r"计算机基础|操作系统|计算机网络|数据库|组成原理|"
    r"基础知识|入门|速查|cheat ?sheet|常见问题|"
    # C++ / Java / Go 语言特性. NOTE: a bare `\bGo\b` was here and had to be removed -
    # it matched the "Go" in "halfrost/LeetCode-Go" and turned a LeetCode solution repo
    # into `fundamentals`. A language name alone does not identify the content type;
    # only specific language-topic terms do below.
    r"C\+\+|cpp|Java|Golang|"
    r"移动语义|右值引用|虚函数|重载|重写|多态|继承|模板|智能指针|内存泄漏|"
    r"野指针|内存碎片|栈溢出|堆溢出|自旋锁|互斥锁|死锁|"
    r"垃圾回收|gc\b|线程池|锁机制|"
    # ML / 大模型 八股考点
    r"transformer|attention|batchnorm|dropout|softmax|交叉熵|反向传播|"
    r"优化器|损失函数|归一化|注意力机制|位置编码", re.I)

# Generic utility tools: a CLI gadget, a wallpaper picker, a phone tracker. These are
# the items the reader means by "与求职面试算法题、各领域实际技术无关的资讯" when they
# arrive from a code host.
#
# SCOPE FIX: `curated list of` / `awesome-list` used to be in here and had to be removed
# from the blanket rule. Measured: it was rejecting `Awesome-Video-Diffusion`,
# `awesome-post-training-RL`, `awesome-vla-wam` - curated PAPER collections, which are
# exactly the 扩展知识 the workbench wants. A curated list is only a tool-list when the
# thing being curated is a tool (wallpapers, dotfiles, CLI apps), so that judgement now
# depends on the curated SUBJECT, handled in is_generic_tool().
GENERIC_TOOL_RE = re.compile(
    r"\b(?:wallpaper|screenshot tool|password manager|bookmark manager|"
    r"file manager|download manager|terminal emulator|dotfiles|"
    r"track(?:s|ing)? (?:location|phone|mobile)|spyware|adblock|"
    r"emoji picker|color picker|font picker|timer app|todo app)\b", re.I)

# A curated list about a RESEARCH or ALGORITHM subject is learning material.
CURATED_LIST_RE = re.compile(
    r"awesome[- ]|curated list|a list of (?:papers|resources|models|datasets)|"
    r"paper list|资源合集|汇总列表", re.I)
CURATED_TECH_SUBJECT_RE = re.compile(
    r"diffusion|vla|vln|vlm|lvlm|llm|language model|video|vision|multimodal|"
    r"post-?training|reinforcement|rlhf|agent|transformer|attention|"
    r"embodied|robot|world model|segment|detection|generation|"
    r"算法|论文|模型|多模态|大模型|强化学习|具身|机器人", re.I)


def is_generic_tool(item: dict) -> bool:
    """Is this a utility tool / non-technical list rather than knowledge?"""
    blob = f"{item.get('title') or ''} {str(item.get('summary') or '')[:300]}"
    if not GENERIC_TOOL_RE.search(blob):
        return False
    # A curated list that is ABOUT a technical subject is kept even if the word
    # "awesome"/"curated list" appears.
    if CURATED_LIST_RE.search(blob) and CURATED_TECH_SUBJECT_RE.search(blob):
        return False
    return True

# ============================================================================
# 6c. SPAM / 灌水 GATE - "面经、八股汇总" that is actually a course advertisement
# ============================================================================
#
# The reader's requirement, verbatim: "注意避免灌水（比如标题为面经、八股汇总，实际是
# 卖课推广等）". This is a real and common pattern in Chinese algorithm/interview content:
# an article ranks under 面经/八股 keywords but its actual payload is a paid course, a
# 知识星球, a training camp, or a QR code to add a sales WeChat.
#
# DESIGN CONSTRAINT: this gate must not punish legitimate content. Selling is not the
# same as mentioning. 代码随想录 genuinely sells a 训练营 and still produces the best free
# algorithm curriculum in Chinese - a rule matching the bare word 训练营 would delete it.
# So a signal only counts as spam when the item has LITTLE substance to offer:
#
#     spam  =  (has an explicit selling/contact signal)
#              AND (carries no substantial technical payload)
#
# The second half is what protects real tutorials. An article that explains KMP for
# 3000 characters while also mentioning its 训练营 at the end is KEEPING material.
SELLING_CTA_RE = re.compile(
    r"扫码|加微信|加\s*V\s*[:：]|vx[:：]|weixin[:：]|私信我|"
    r"限时(?:优惠|特价|折扣|活动)|原价|立减|仅需\s*\d|券后|"
    r"点击购买|立即购买|拼团|"
    # NOTE: bare 秒杀 and 下单 were here and are NOT sales words in this domain - 秒杀系统
    # design is a standard interview topic ("点赞用 ZSet…秒杀结束后数据怎么同步"), and
    # 下单 appears in e-commerce system design. Measured: they flagged the legitimate
    # 面经 "极兔后端一面". Sales intent needs the promotional compound, not the bare verb.
    r"秒杀价|限时秒杀|下单立减|立即下单|"
    r"付费(?:专栏|课程|社群)|收费(?:课程|社群)|会员(?:专享|福利)|"
    r"知识星球|小报童|训练营(?:报名|开营|招生|仅|最后)|"
    r"课程(?:报名|咨询|优惠|价格)|"
    r"团队(?:微信|联系方式)|商务(?:合作|联系)|"
    r"代充|租号|账号购买|"
    # Off-topic commercial spam that keyword-matched its way into a 面经 category.
    # Found in the corpus: two nowcoder posts advertising 品茶 services, classified into
    # `coding` because their titles contained none of the usual signals.
    r"品茶|茶工作室|海选|技师|上门服务|会所|桑拿|"
    r"贷款|办卡|刷单|兼职日结|引流|第一现场.{0,8}工作室",
    re.I)

# Commerce words that are only weak evidence on their own, because they also appear in
# legitimate technical writing ("我们订阅了三种推理服务做对比"). Used with a length and
# technical-payload condition, never alone.
COMMERCIAL_RE = re.compile(
    r"套餐|订阅|续费|充值|试用(?:账号|额度)|免费额度|"
    r"价格(?:对比|一览|多少)|按使用强度选|怎么接入|如何接入|"
    r"官网(?:价格|入口)|性价比|选购|激活码|破解|白嫖",
    re.I)

# Content that is commercial or junk no matter how long it is: nothing about it can be
# learned for an algorithm interview.
SPAM_ONLY_RE = re.compile(
    r"品茶|茶工作室|海选|技师|上门服务|会所|桑拿|"
    r"贷款|办卡|刷单|兼职日结|引流|第一现场.{0,8}工作室",
    re.I)

# A pricing/tutorial card is spam when its payload is purchase guidance rather than a
# mechanism. Checked separately from SELLING_SIGNAL_RE because these words legitimately
# appear inside real technical writing ("我们对比了三种量化方案的显存开销").
PRICING_GUIDANCE_RE = re.compile(
    r"套餐|订阅|续费|充值|代充|租号|试用账号|免费额度|价格对比|怎么接入|如何接入|"
    r"激活码|破解|白嫖|选择建议|选购|性价比",
    re.I)

# Phrases that signal a genuine explanation is present, regardless of the selling.
TECH_PAYLOAD_RE = re.compile(
    r"时间复杂度|空间复杂度|复杂度为|O\(n|算法思路|解题思路|思路如下|代码实现|"
    r"示例\s*\d|输入[:：]|输出[:：]|推导|原理|定义[:：]|区别[:：]|"
    r"为什么|原因在于|实现方式|步骤[:：]|源码|样例|"
    r"recursion|complexity|we propose|implementation", re.I)


def spam_reason(item: dict) -> str:
    """Return a human-readable reason if this looks like 灌水/推广, else "".

    SCOPE LESSON (measured twice, both false positives on real corpus content):
      · Matching commercial words anywhere in the text flagged the legitimate 八股 card
        "4.2.2 银行技术面---技术基础类问题（操作系统）" because its body contains the word
        "订阅". A commercial word inside a technical sentence is not a signal.
      · Matching bare domain words flagged real VLM papers.
    So the rule is now: commerce must be the SUBJECT (a title-level claim), or the
    selling language must dominate a very short body. Substantial technical payload
    always wins.
    """
    title = str(item.get("title") or "")
    summary = str(item.get("summary") or "")
    blob = f"{title} {summary}"

    tech_hits = len(set(m.group(0).lower() for m in TECH_PAYLOAD_RE.finditer(blob)))

    # (a) an explicit selling call-to-action anywhere is strong evidence
    if SELLING_CTA_RE.search(blob):
        # A long page dense with technical markers is teaching that happens to sell.
        # Density (marker occurrences), not the count of DISTINCT markers, is what
        # separates a tutorial from a store page: a real write-up repeats 时间复杂度 /
        # 代码实现 / 示例 many times, while an ad mentions its one technical word once.
        tech_total = len(TECH_PAYLOAD_RE.findall(blob))
        if len(summary) >= 600 and tech_total >= 6:
            return ""                   # substantial teaching that happens to sell
        hits = len(set(m.group(0).lower() for m in SELLING_CTA_RE.finditer(blob)))
        return f"疑似卖课/推广（推销话术 {hits} 处，技术内容不足）"

    # (b) commerce as the TITLE subject, with no real mechanism behind it
    if PRICING_GUIDANCE_RE.search(title) and tech_hits < 3:
        return "购买/接入指引类内容（非知识积累）"

    # (c) commercial language dominating a very short body
    comm = len(set(m.group(0).lower() for m in COMMERCIAL_RE.finditer(blob)))
    if comm >= 3 and len(summary) < 220 and tech_hits < 2:
        return f"疑似推广（商业词 {comm} 处，正文过短且无技术内容）"

    # (d) commercial spam that is not a technical topic at all
    if SPAM_ONLY_RE.search(blob):
        return "推广/垃圾内容（非知识积累）"
    return ""

# 牛客题霸 is a big graded set that includes HARDWARE tracks (FPGA/数字电路: 优先编码器、
# 译码器、时序电路、触发器). Those are real 编程题 but they are irrelevant to an
# algorithm-intern candidate, and position-based sampling cannot separate them because
# the set is ordered by category: measured, one slice returned eleven consecutive
# FPGA problems. So candidate problems are filtered by CONTENT.
#
# The allow-list is deliberately about algorithms and ML/DL implementation, and the
# reject-list is about hardware design. Unknown material is kept (a new topic should be
# discovered, not silently dropped) - only explicit hardware signals are excluded.
PROBLEM_RELEVANT_RE = re.compile(
    r"链表|二叉树|二叉搜索树|树的遍历|前序|中序|后序|层序|栈|队列|堆|哈希|散列|"
    r"排序|查找|二分|双指针|滑动窗口|递归|回溯|动态规划|贪心|分治|并查集|"
    r"图|最短路径|拓扑|最小生成树|字符串|数组|矩阵|位运算|位操作|前缀和|差分|"
    r"单调栈|字典树|trie|kmp|背包|排列|组合|子集|子序列|最长|回文|括号|"
    r"大数|进制|质数|最大公约数|最小公倍数|概率|期望|随机|"
    r"矩阵乘|卷积|激活|softmax|attention|transformer|梯度|反向传播|"
    r"神经网络|损失|优化器|归一化|注意力|量化|推理|吞吐|显存|"
    r"torch|numpy|python|c\+\+|javascript|链表|排序算法",
    re.I)
PROBLEM_EXCLUDE_RE = re.compile(
    r"编码器|译码器|触发器|时序电路|逻辑电路|门电路|寄存器|计数器|"
    r"多路器|数据选择器|全加器|半加器|奇偶校验|verilog|vhdl|fpga|"
    r"状态转移|卡诺图|布尔|与非门|或非门|d触发器|t触发器|jk触发器|cmos|"
    r"走线|管脚|时钟树|复位信号|亚稳态",
    re.I)

EVIDENCE_KEYS = ("category", "categoryEvidence", "knowledge")


def classify_knowledge_type(item: dict, cat: str) -> str:
    """One of: job, interview, coding, fundamentals, method, news, opinion, other.

    ORDER IS THE DESIGN. First match wins, and the ordering encodes what calibrating
    this on the real 664-item corpus taught:

      · A RESEARCH PAPER MUST BE CLASSIFIED AS RESEARCH. An earlier ordering tested
        `interview` before `method`, and since paper abstracts routinely contain the
        words "interview", "scaling" and "benchmark", genuine papers came back as
        interview material - RPTune, Adaptive Reward Routing and Homomorphic Advantage
        Operator were all labelled `interview`. So the SOURCE and the venue are checked
        first, before any text pattern.
      · A RELEASE NOTE THAT EXPLAINS A MECHANISM IS DEPTH, so `method` is still tested
        before `news`; and `news` before `interview`, so an acquisition story full of
        the word "engineer" is not mistaken for a write-up.
    """
    kind = str(item.get("kind") or "")
    if kind == "job":
        return "job"

    chan = str(item.get("sourceId") or item.get("channel") or "")
    title = str(item.get("title") or "")

    # --- 1. explicit interview/coding titles win outright -------------------------
    if EXPLICIT_INTERVIEW.search(title):
        # A title that is explicitly a 面经/挂经 is interview material - UNLESS it is
        # framed as a fundamentals collection ("技术面试基础知识总结"), which is 八股.
        if not EXPLICIT_FUNDAMENTALS.search(title):
            return "interview"
    # Fundamentals before coding: a 牛客 title is "[单选题] …以下哪项…", so the
    # multiple-choice marker must be tested before the generic "以下哪项" phrasing or
    # every 八股 question lands in the algorithm-problem bucket.
    if EXPLICIT_FUNDAMENTALS.search(title):
        return "fundamentals"
    # Fundamentals MUST come before the coding patterns: a 牛客 title reads
    # "[单选题] …以下哪项…", and the "以下哪项" phrasing is a coding-pattern match.
    if EXPLICIT_FUNDAMENTALS.search(title):
        return "fundamentals"
    if EXPLICIT_CODING.search(title):
        return "coding"

    # --- 2. authoritative source signals -----------------------------------------
    if item.get("peerReviewed") or chan in ("arxiv", "hf_papers", "s2", "openalex",
                                            "crossref", "openreview"):
        return "method"
    if chan.startswith("jobs_"):
        return "job"
    if chan in ("boss", "lagou", "shixiseng"):
        return "job"          # login-walled job boards: only postings are imported

    # --- 3. code projects are learning resources, not noise -----------------------
    # IMPORTANT calibration lesson: an earlier version of the evidence gate rejected
    # `huggingface/diffusers`, `mlc-ai/web-llm` and `genkit` because a README does not
    # read like a mechanism explanation. But a widely-used framework IS legitimate
    # 扩展知识 - you learn the field's tooling from it. So a repo is treated as a
    # knowledge type of its own, and only explicitly generic utility tools are
    # excluded by the gate later.
    is_repo = (chan in ("github", "gh_trending")
               or bool(item.get("stars")) or "github.com" in str(item.get("url") or ""))
    if is_repo:
        if is_generic_tool(item):
            return "other"            # CLI gadget / wallpaper / tracker: not knowledge
        return "project"

    # --- 4. text patterns over title + summary -----------------------------------
    blob = " ".join([title, str(item.get("summary") or "")[:400],
                     " ".join(item.get("tags") or [])])
    for name, pat in KNOWLEDGE_TYPE_RULES:
        if not pat.search(blob):
            continue
        # SCOPE FIX: a `fundamentals` MATCH ON THE SUMMARY IS NOT EVIDENCE, because the
        # fundamentals markers include generic phrases like 数据结构 and 基础 — which
        # appear in the description of every algorithm repository. Measured: this made
        # `halfrost/LeetCode-Go` and `youngyangyang04/leetcode-master` (both LeetCode
        # solution repos!) come back as fundamentals instead of coding. So a non-method
        # classification from summary text is only accepted when the TITLE independently
        # supports it; otherwise the specific title-level rules above already had their
        # chance and the generic text should not override them.
        if name != "method" and not any(p.search(title) for _, p in KNOWLEDGE_TYPE_RULES):
            continue
        return name
    # A long abstract from a research category is research material even when it
    # avoids every phrase above.
    if cat in ("multimodal", "posttraining", "worldmodel", "generative", "rl",
               "foundation") and len(str(item.get("summary") or "")) >= 300:
        return "method"
    return "other"


def category_evidence(cat_hits: list, cfg: dict) -> float:
    """Category match strength, weighted by how DISCRIMINATING each keyword is.

    The old formula was `8.0 * len(hits)`: five generic hits scored the same as five
    specific ones, and a single generic hit still collected 8 points on a 100-point
    scale. Now each hit is worth 5-14 points depending on specificity, so weak
    evidence cannot carry an item on its own.
    """
    if not cat_hits:
        return 0.0
    total = 0.0
    for kw in cat_hits:
        low = str(kw).lower()
        if low in GENERIC_KEYWORDS or len(low) <= 3:
            total += 5.0                 # the category's own name: weak evidence
        elif re.search(r"[\u4e00-\u9fff]", low) or "-" in low or " " in low:
            total += 12.0                # multi-word / CJK terms are specific
        else:
            total += 8.0
    return min(45.0, total)


def knowledge_value(item: dict, ktype: str, cfg: dict) -> float:
    """How much is this worth ACCUMULATING in a job-hunting study workbench?

    Deliberately opinionated and data-driven from the reader's own statement: they
    want depth, interview material, fundamentals and postings, and they explicitly do
    not want news that merely happens to be recent or adjacent.
    """
    table = (cfg.get("scoring") or {}).get("knowledgeValue") or {}
    default = {"method": 16.0, "coding": 14.0, "interview": 14.0,
               "fundamentals": 12.0, "job": 13.0,
               # A widely-used framework/implementation is legitimate 扩展知识: it is
               # how you learn the field's tooling. Ranked just under research.
               "project": 10.0,
               "other": 2.0, "news": -6.0, "opinion": -10.0}
    return float(table.get(ktype, default.get(ktype, 0.0)))


def weak_enough_ok(cat_hits: list, cfg: dict) -> bool:
    """Is single/weak keyword evidence acceptable for low-value material?

    Opinion pieces and rants have some value in a job-hunt context (a 面经 can read
    like a rant), but only when they carry depth-type evidence. This keeps the gate
    readable instead of nesting the same condition twice at the call site.
    """
    return any(str(h).lower() not in GENERIC_KEYWORDS for h in (cat_hits or []))


def score_item(item, cfg, tier, cat_hits):
    sc = cfg.get("scoring") or {}
    parts = {}
    # Category evidence: specificity-weighted instead of a flat 8/keyword, so one
    # generic hit (the category's own name) can no longer carry an item on its own.
    parts["category"] = category_evidence(cat_hits, cfg)
    # What KIND of material this is - the reader's actual priority, as data.
    ktype = classify_knowledge_type(item, item.get("category") or "")
    parts["knowledge"] = knowledge_value(item, ktype, cfg)
    # An item that matched NO category keyword loses the category component entirely
    # and takes an extra penalty, so it ranks below every genuinely classified item.
    if not cat_hits:
        parts["unclassified"] = -12.0

    # Freshness. Lowered from 30 to a maximum of 22 and made type-aware: recency is a
    # TIE-BREAKER, not the main reason to admit something. News decays fast by design
    # (it is worth little next week anyway), while a mechanism write-up stays useful
    # for months, so depth keeps its freshness credit much longer.
    hl = float(sc.get("freshnessHalfLifeDays", 7.0))
    if ktype in ("method", "fundamentals"):
        hl *= 4.0                      # reference material: relevant for months
    elif ktype in ("interview", "job", "coding"):
        hl *= 1.5                      # perishable, but worth a little more than news
    elif ktype == "news":
        hl *= 0.4                      # news is stale almost immediately
    ts = parse_ts(item.get("publishedAt"))
    if ts and hl > 0:
        age = max(0.0, (now_cst() - ts).total_seconds() / 86400.0)
        parts["freshness"] = 22.0 * (0.5 ** (age / hl))
    else:
        parts["freshness"] = 4.0

    parts["source"] = 12.0 * float((sc.get("sourceTierWeight") or {}).get(tier, 0.7))

    q = item.get("qualitySignals") or {}
    boost = float(sc.get("qualityBoost", 8.0))
    qs = 0.0
    if item.get("peerReviewed"):
        qs += boost
    stars = item.get("stars") or q.get("stars") or 0
    if stars:
        qs += min(boost, 0.8 * (float(stars) ** 0.5))
    up = item.get("upvotes") or q.get("upvotes") or 0
    if up:
        qs += min(boost * 0.75, float(up) ** 0.65 / 2.0)
    if item.get("codeAvailable") or q.get("codeAvailable"):
        qs += 2.0
    parts["quality"] = min(boost * 2.2, qs)

    parts["content"] = min(9.0, len(str(item.get("summary") or "")) / 220.0 * 9.0)

    total = sum(parts.values())
    if item.get("lang") == "zh":
        total *= 1.04
    return round(min(100.0, total), 1), {k: round(v, 1) for k, v in parts.items()}


def explain(item, hits, cfg):
    """'Why it matters' is composed ONLY from observed signals, never inferred."""
    czh = (cfg["categories"].get(item.get("category")) or {}).get("zh", "综合趋势")
    bits = []
    if hits:
        bits.append("命中关键词 " + "、".join(hits[:5]))
    if item.get("peerReviewed"):
        bits.append(f"同行评审 ({item.get('venue') or 'conference'})")
    if item.get("stars"):
        bits.append(f"GitHub {item['stars']} stars")
    if item.get("upvotes"):
        bits.append(f"社区热度 {item['upvotes']}")
    if item.get("codeAvailable"):
        bits.append("疑似附带开源实现")
    if item.get("lang") == "zh":
        bits.append("中文来源，贴近国内求职语境")
    return f"归属「{czh}」方向" + ("；" + "；".join(bits) if bits else "；待人工复核相关性")


# ============================================================================
# 7. NORMALIZE + DEDUPE
# ============================================================================
def _sources_of(item):
    srcs = []
    if item.get("url"):
        srcs.append({"url": item["url"], "name": item.get("sourceId") or item.get("channel")})
    if item.get("canonicalUrl") and item["canonicalUrl"] != item.get("url"):
        srcs.append({"url": item["canonicalUrl"], "name": "canonical"})
    return srcs


def normalize(raw, cfg, tier):
    if not isinstance(raw, dict):
        return None
    title = clean_text(raw.get("title") or "", 400)
    if not title:
        return None
    url = canonical_url(raw.get("url") or "")
    canon = canonical_url(raw.get("canonicalUrl") or "")
    if not url and not canon:
        return None
    item = {
        "sourceId": raw.get("sourceId") or raw.get("channel") or "unknown",
        "channel": raw.get("channel") or raw.get("sourceId") or "unknown",
        "externalId": str(raw.get("externalId") or title_key(title)),
        "title": title,
        "summary": clean_text(raw.get("summary") or "", 2000),
        "url": url or canon,
        "canonicalUrl": canon or None,
        "authors": [clean_text(a, 80) for a in (raw.get("authors") or []) if a][:14],
        "publishedAt": raw.get("publishedAt"),
        "fetchedAt": iso(now_cst()),
        "lang": raw.get("lang") or ("zh" if re.search(r"[\u4e00-\u9fff]", title) else "en"),
        "tags": [clean_text(t, 40) for t in (raw.get("tags") or []) if t][:12],
        "venue": raw.get("venue") or None,
        "ccf": raw.get("ccf") or None,
        "peerReviewed": bool(raw.get("peerReviewed")),
        "codeAvailable": bool(raw.get("codeAvailable") or (raw.get("qualitySignals") or {}).get("codeAvailable")),
        "stars": raw.get("stars"),
        "upvotes": raw.get("upvotes"),
        "qualitySignals": raw.get("qualitySignals") or {},
        "difficulty": raw.get("difficulty"),
        "collectorVersion": COLLECTOR_VERSION,
        "contentHash": sha1(f"{title}|{raw.get('summary') or ''}")[:16],
    }
    item["id"] = stable_id(item["sourceId"], item["externalId"])
    cat, hits = classify(item, cfg)
    item["category"] = cat
    item["categoryHits"] = hits
    item["knowledgeType"] = classify_knowledge_type(item, cat)
    rel, breakdown = score_item(item, cfg, tier, hits)
    item["relevanceScore"] = rel
    item["relevanceBreakdown"] = breakdown
    item["sources"] = _sources_of(item)
    item["why"] = explain(item, hits, cfg)
    return item


class Deduper:
    """Layered de-duplication: stable id -> canonical url -> title -> simhash."""

    def __init__(self, state, cfg):
        d = cfg.get("dedupe") or {}
        self.bits = int(d.get("simhashBits", 64))
        self.threshold = int(d.get("hammingThreshold", 3))
        self.by_key = state.setdefault("byKey", {})
        self.by_url = state.setdefault("byUrl", {})
        self.by_title = state.setdefault("byTitle", {})
        self.hashes = state.setdefault("hashes", [])

    def lookup(self, item):
        uid = f"{item.get('sourceId')}::{item.get('externalId')}"
        if uid in self.by_key:
            return self.by_key[uid]
        cu = item.get("canonicalUrl") or item.get("url") or ""
        if cu and cu in self.by_url:
            return self.by_url[cu]
        tk = title_key(item.get("title") or "")
        if tk and tk in self.by_title:
            return self.by_title[tk]
        h = simhash(f"{item.get('title')} {item.get('summary')}"[:1500], self.bits)
        item["_simhash"] = h
        if h:
            for other, cid in self.hashes:
                if hamming(h, other) <= self.threshold:
                    return cid
        return None

    def register(self, item, canonical_id):
        self.by_key[f"{item.get('sourceId')}::{item.get('externalId')}"] = canonical_id
        cu = item.get("canonicalUrl") or item.get("url") or ""
        if cu:
            self.by_url[cu] = canonical_id
        tk = title_key(item.get("title") or "")
        if tk:
            self.by_title[tk] = canonical_id
        h = item.pop("_simhash", None) or simhash(
            f"{item.get('title')} {item.get('summary')}"[:1500], self.bits)
        if h:
            self.hashes.append([h, canonical_id])

    def prune(self, max_entries=40000):
        if len(self.hashes) > max_entries:
            self.hashes = self.hashes[-max_entries:]
        for store, cap in ((self.by_key, 60000), (self.by_url, 60000), (self.by_title, 60000)):
            if len(store) > cap:
                for k in list(store.keys())[:len(store) - cap]:
                    store.pop(k, None)


def merge_duplicate(existing, incoming):
    """Same content seen on several channels: keep the richest field, keep all sources."""
    out = dict(existing)
    for field in ("title", "summary"):
        if len(str(incoming.get(field) or "")) > len(str(out.get(field) or "")):
            out[field] = incoming[field]
    for field in ("authors", "tags"):
        merged = list(dict.fromkeys(list(out.get(field) or []) + list(incoming.get(field) or [])))
        if merged:
            out[field] = merged[:16]
    qs = dict(out.get("qualitySignals") or {})
    for k, v in (incoming.get("qualitySignals") or {}).items():
        if isinstance(v, (int, float)) and isinstance(qs.get(k), (int, float)):
            qs[k] = max(qs[k], v)
        else:
            qs.setdefault(k, v)
    if qs:
        out["qualitySignals"] = qs
    out["peerReviewed"] = bool(out.get("peerReviewed") or incoming.get("peerReviewed"))
    out["codeAvailable"] = bool(out.get("codeAvailable") or incoming.get("codeAvailable"))
    srcs = list(out.get("sources") or [])
    have = {s.get("url") for s in srcs}
    for s in _sources_of(incoming):
        if s.get("url") and s["url"] not in have:
            srcs.append(s)
            have.add(s["url"])
    out["sources"] = srcs[:6]
    if incoming.get("publishedAt") and (not out.get("publishedAt")
                                       or incoming["publishedAt"] < out["publishedAt"]):
        out["publishedAt"] = incoming["publishedAt"]
    out["mergedFrom"] = list(dict.fromkeys(list(out.get("mergedFrom") or []) + [incoming.get("sourceId")]))
    out["updatedAt"] = iso(now_cst())
    return out


# ============================================================================
# 8. STORAGE
# ============================================================================
def atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    pretty = path.name == "runs.json"
    data = json.dumps(payload, ensure_ascii=False,
                      indent=1 if pretty else None,
                      separators=None if pretty else (",", ":"))
    tmp.write_text(data, "utf-8")
    if tmp.stat().st_size < 2:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"refusing to write a suspiciously small file: {path}")
    os.replace(tmp, path)          # atomic on the same volume


def read_json(path: Path, fallback):
    try:
        if path.exists():
            return json.loads(path.read_text("utf-8"))
    except Exception:
        pass
    return fallback


def build_all_items(state, fresh, cfg=None):
    canonical = state.get("canonical") or {}
    merged = dict(canonical)
    for item in fresh:
        merged[item["id"]] = {**merged.get(item["id"], {}), **item}
    keep = ("id", "sourceId", "channel", "externalId", "category", "title", "summary",
            # summarySource records HOW a summary was obtained (feed vs page meta
            # tag). Without it in this whitelist the provenance was silently
            # dropped on every rebuild, so nothing could tell an original abstract
            # from a backfilled description.
            "summarySource",
            # relevanceBreakdown exposes WHY an item scored what it did, including
            # the learning-signal bonus. It must survive the rebuild or the ranking
            # change is invisible and unauditable.
            "relevanceBreakdown",
            # knowledgeType drives the ranking weight and the reader-facing label;
            # without it in this whitelist every rebuild would silently drop it.
            "knowledgeType",
            "keyPoints", "why", "url", "canonicalUrl", "sources", "authors", "publishedAt",
            "fetchedAt", "lang", "tags", "entities", "difficulty", "relevanceScore",
            "qualitySignals", "venue", "ccf", "peerReviewed", "codeAvailable", "stars",
            "upvotes", "firstSeen", "lastSeen", "contentHash", "categoryHits")
    # Honour the blocklist here as well as on the ingest path.
    #
    # Two gates are needed, not one. Filtering only the incoming raw items stops
    # NEW imports, but state["canonical"] already holds whatever was imported
    # before, so items that slipped in during an earlier run would stay published
    # forever. Rebuilding from `canonical` is exactly how that happened: the
    # library sat at 763 with 209 human-removed items still listed.
    blocked_ids = set(state.get("blockedIds") or [])
    # Drop `unclassified` items from the published corpus.
    #
    # WHY: an item with an empty `categoryHits` matched NO keyword for any tracked
    # direction, so it cannot serve any of them - measured, that bucket is off-topic
    # news (BYD industry pieces, a chemistry response model, a location-tracking CLI,
    # Cursor plugin specs, EU AI Act commentary). The reader asked explicitly for
    # "no news unrelated to job-hunting, interview prep or real technical topics", and
    # these were 85 of 720 items while also being 11.8% of all attention.
    #
    # They are dropped from the OUTPUT only, not from `state["canonical"]`. That
    # matters: if keywords improve later, `--rescore` reclassifies the stored items
    # and the good ones come back automatically. The cost of keeping them in state is
    # a few hundred KB, which is cheap insurance against an irreversible decision.
    drop_unclassified = bool((((cfg or {}).get("limits")) or {}).get("dropUnclassified", True))
    out = []
    for item in merged.values():
        if blocked_ids and item.get("id") in blocked_ids:
            continue
        if drop_unclassified and item.get("category") == "unclassified":
            continue
        row = {k: item.get(k) for k in keep if item.get(k) is not None}
        row.setdefault("sources", [])
        row["date"] = str(item.get("firstSeen") or item.get("fetchedAt") or "")[:10]
        out.append(row)
    out.sort(key=lambda x: -(x.get("relevanceScore") or 0))
    return out


def compute_stats(items):
    by_cat, by_ch = {}, {}
    for i in items:
        by_cat[i.get("category", "trend")] = by_cat.get(i.get("category", "trend"), 0) + 1
        by_ch[i.get("channel", "unknown")] = by_ch.get(i.get("channel", "unknown"), 0) + 1
    return {"byCategory": by_cat, "byChannel": by_ch,
            "zhShare": round(sum(1 for i in items if i.get("lang") == "zh") / max(1, len(items)), 3)}


# ============================================================================
# 9. RUN
# ============================================================================
# Ordering for the human-facing channel table: things that worked first, then
# empty, then broken, then blocked, then never-verified.
STATE_RANK = {"ok": 0, "partial": 1, "empty": 2, "error": 3, "parse_error": 4,
              "blocked": 5, "unknown": 6}
TIER_RANK = {"P0": 0, "P1": 1, "P2": 2, "MANUAL": 3}


def write_proposals(cfg, results, fresh, all_items, log) -> dict:
    """Deterministic self-iteration output.

    The LLM deep-read layer can write richer proposals, but it needs a
    credential and is therefore optional. This function makes the
    observe -> propose step unconditional: every run leaves a reviewable file
    containing only *mechanically derived* suggestions (no model inference, no
    guesses). A human still approves anything before it takes effect.

    Signals used (all observable without an LLM):
      * channels that keep failing / are blocked / returned parse errors
      * categories that received no items at all
      * categories that got items historically but none this run
      * configured keywords that never hit anything (dead weight)
      * source hosts outside the known-host list (possible junk ingestion)
    """
    proposals_dir = DATA_DIR / "proposals"
    day = date_key(now_cst())

    runs = read_json(RUNS_PATH, [])
    if not isinstance(runs, list):
        runs = []
    fail_streak: dict = {}
    for r in runs[:8]:
        for part in str(r.get("notes") or "").split(";"):
            part = part.strip()
            m = re.match(r"^([\w\-]+)=(error|blocked|parse_error|empty)$", part)
            if m and m.group(2) in ("error", "blocked", "parse_error"):
                fail_streak[m.group(1)] = fail_streak.get(m.group(1), 0) + 1

    channel_health = []
    for cid, rec in sorted(results.items()):
        status = str(rec.get("status"))
        if status == "ok":
            continue
        streak = fail_streak.get(cid, 0) + (1 if status in ("error", "blocked", "parse_error") else 0)
        action = {
            "blocked": "确认是否已被反爬或已下线；如需继续，改为人工导入或寻找替代源",
            "error": "检查接口变更或凭据；连续 3 次失败应降级到备用源",
            "parse_error": "响应格式变了，需要更新解析逻辑（这是我们的问题，不是站点的问题）",
            "empty": "查询式可能过窄，考虑放宽关键词或时间窗口",
        }.get(status, "人工复核")
        channel_health.append({
            "id": cid, "nameZh": rec.get("nameZh") or cid, "status": status,
            "streak": streak, "lastError": rec.get("error"),
            "suggestedAction": action, "escalate": streak >= 3,
        })
    channel_health.sort(key=lambda c: (-int(c["escalate"]), -c["streak"], c["id"]))

    got: dict = {}
    for it in all_items:
        got[it.get("category")] = got.get(it.get("category"), 0) + 1
    got_fresh: dict = {}
    for it in fresh:
        got_fresh[it.get("category")] = got_fresh.get(it.get("category"), 0) + 1
    empty_categories = [cid for cid in cfg["categories"] if not got.get(cid)]
    quiet_categories = [cid for cid in cfg["categories"] if got.get(cid) and not got_fresh.get(cid)]

    hits_counter: dict = {}
    for it in all_items:
        for kw in (it.get("categoryHits") or []):
            hits_counter[kw] = hits_counter.get(kw, 0) + 1
    dead_keywords: dict = {}
    for cid, c in cfg["categories"].items():
        kws = c.get("keywords") or []
        dead = [k for k in kws if k not in hits_counter and len(k) > 4]
        if dead and len(dead) > max(3, len(kws) // 2):
            dead_keywords[cid] = dead[:15]

    suspicious_hosts: dict = {}
    for it in all_items:
        h = urllib.parse.urlsplit(it.get("url") or "").netloc.lower().replace("www.", "")
        if h and not any(h == k or h.endswith("." + k) for k in KNOWN_HOSTS):
            suspicious_hosts[h] = suspicious_hosts.get(h, 0) + 1
    suspicious_hosts = dict(sorted(suspicious_hosts.items(), key=lambda kv: -kv[1])[:12])

    # ------------------------------------------------------------------
    # New-channel proposals.
    #
    # This field existed but was hardcoded to an empty list, so the workbench could
    # never propose a new source - the "channel-add" rule in the design (a domain
    # that keeps showing up in manual imports but is not collected automatically)
    # had no implementation.
    #
    # Signal used: domains that appear in HUMAN-IMPORTED items (channel == "inbox")
    # and are not in KNOWN_HOSTS. That is the strongest available evidence that a
    # source is worth collecting: the reader went and fetched it by hand, 5+ times.
    # Nothing is fetched or enabled automatically - this only produces a proposal
    # that the deep-read agent verifies (API/RSS availability, robots.txt) before a
    # human approves it.
    # ------------------------------------------------------------------
    new_channel_proposals = []
    try:
        imported_hosts: dict = {}
        for it in all_items:
            if it.get("channel") != "inbox":
                continue
            h = urllib.parse.urlsplit(it.get("url") or "").netloc.lower().replace("www.", "")
            if not h:
                continue
            if any(h == k or h.endswith("." + k) for k in KNOWN_HOSTS):
                continue
            rec = imported_hosts.setdefault(h, {"count": 0, "samples": [], "categories": {}})
            rec["count"] += 1
            if len(rec["samples"]) < 3:
                rec["samples"].append(str(it.get("title") or "")[:90])
            c = it.get("category") or "trend"
            rec["categories"][c] = rec["categories"].get(c, 0) + 1
        for h, rec in sorted(imported_hosts.items(), key=lambda kv: -kv[1]["count"]):
            if rec["count"] < 5:
                continue
            top_cat = max(rec["categories"].items(), key=lambda kv: kv[1])[0]
            new_channel_proposals.append({
                "domain": h,
                "importedCount": rec["count"],
                "dominantCategory": top_cat,
                "sampleTitles": rec["samples"],
                "suggestedMode": "rss-or-api",
                "reason": (f"你手工导入了 {rec['count']} 条来自 {h} 的内容，但它不在采集渠道里。"
                           f"若该站 robots.txt 允许且有 RSS/API，建议加入渠道自动采集。"),
                "requiresHumanApproval": True,
                "verificationSteps": [
                    "检查 robots.txt 是否允许抓取",
                    "确认是否有官方 RSS 或公开 API（优先）",
                    "若无 API/RSS，评估 HTML 解析的稳定性与限速要求",
                    "批准后在 CHANNEL_SPECS 与 config 中登记，并跑一次 --probe 实测",
                ],
            })
    except Exception as e:
        log(f"new-channel proposal scan failed: {e}", "warn")

    queue = []
    escalated = [c for c in channel_health if c["escalate"]]
    if escalated:
        queue.append("以下渠道已连续 3 轮以上失败，请确认是永久下线还是需要换源："
                     + "，".join(f"{c['id']}({c['status']}×{c['streak']})" for c in escalated))
    if empty_categories:
        queue.append("以下分类至今没有任何条目，请确认关键词是否过窄或渠道是否缺失："
                     + "，".join(empty_categories))
    if dead_keywords:
        queue.append("以下分类有过半关键词从未命中，建议精简或改写："
                     + "，".join(f"{k}({len(v)} 个)" for k, v in dead_keywords.items()))
    if len(suspicious_hosts) > 8:
        queue.append("出现较多未知来源域名，请抽查是否误抓：" + "，".join(list(suspicious_hosts)[:6]))

    payload = {
        "date": day,
        "generatedAt": iso(now_cst()),
        "generatedBy": "collect.py deterministic layer (no LLM required)",
        "reviewPolicy": {
            "autoApplicable": ["单个关键词权重 ≤10% 的微调"],
            "requiresHumanApproval": ["分类增删改名", "渠道增删与升降级", "评分公式与权重",
                                      "去重阈值", "blocklist", "robots.txt 状态变化"],
        },
        "channelHealth": channel_health,
        "coverage": {
            "categoriesWithItems": sorted(got.keys()),
            "categoriesEmpty": empty_categories,
            "categoriesQuietThisRun": quiet_categories,
            "byCategoryTotal": got,
            "byCategoryFresh": got_fresh,
        },
        "keywordProposals": [
            {"category": cid, "remove": kws,
             "reason": "这些关键词在全部已入库条目中从未命中，占用匹配与打分开销"}
            for cid, kws in dead_keywords.items()
        ],
        "newChannelProposals": new_channel_proposals,
        "noiseReport": {
            "unknownHosts": suspicious_hosts,
            "unknownHostItemShare": round(sum(suspicious_hosts.values()) / max(1, len(all_items)), 3),
        },
        "humanReviewQueue": queue,
        "stats": {
            "totalItems": len(all_items),
            "freshThisRun": len(fresh),
            "channelsRan": len(results),
            "channelsOk": sum(1 for r in results.values() if r.get("status") == "ok"),
        },
    }

    atomic_write_json(proposals_dir / f"{day}.json", payload)
    atomic_write_json(proposals_dir / "latest.json", payload)
    if queue:
        log(f"proposals/{day}.json: {len(queue)} 条待人工复核", "warn")
    else:
        log(f"proposals/{day}.json: 无需人工介入", "ok")
    return payload


def classify_failure(exc_text: str) -> str:
    """Turn an exception repr into a status the operator can act on.

    `parse_error` exists so that "the host answered and our parser choked" is
    never confused with `empty` ("the host answered with nothing"): the first is
    our bug and must be logged as such, the second is normal for a quiet day.
    """
    low = (exc_text or "").lower()
    if any(k in low for k in ("403", "401", "451", "forbidden", "unauthorized")):
        return "blocked"
    if "429" in low or "too many requests" in low:
        return "blocked"
    if any(k in low for k in ("parseerror", "jsondecode", "not well-formed", "syntaxerror",
                              "mismatched tag", "undefined entity", "unreadable", "xml")):
        return "parse_error"
    return "error"


def build_channel_result(cid, spec, status, count, err, dur, risk_note="",
                         backup_used=None, backup_attempts=None):
    return {
        "id": cid, "nameZh": CHANNEL_NAMES.get(cid, cid), "tier": spec.get("tier", "P2"),
        "mode": spec.get("mode", "api"), "authRequired": bool(spec.get("auth")),
        "status": status, "count": count, "error": err, "durationSec": round(dur, 2),
        "backup": spec.get("backup", []),
        # `backup` above documents the alternatives; these two record what the
        # runner actually DID this pass, so the UI can show a real fallback chain
        # instead of a promise. Surviving only as a label was a genuine gap.
        "backupUsed": backup_used,
        "backupAttempts": backup_attempts or [],
        "riskNote": risk_note or None,
        "disabled": cid in DISABLED_CHANNELS,
        "checkedAt": iso(now_cst()), "lastChecked": iso(now_cst()),
    }


def collect_channels(wanted, cfg, per_channel, log):
    """Run the requested collectors, isolating every failure.

    Executes each channel's declared `backup` chain for real: when the primary
    collector raises or returns nothing, the alternatives in
    CHANNEL_SPECS[cid]["backup"] are tried in order until one yields items. That
    turns the reliability spec's "备用来源" from documentation into behaviour.
    Returns (results, raw_items).
    """
    results, raw_items = {}, []

    # Per-channel allowance. A flat cap fought the reader's stated priority: 面经 and
    # 算法题 arrive mostly through `nowcoder`, and capping it at the global 12 kept
    # `coding` at 56 items while `agent` grew to 130 - even though interview material
    # is the most perishable content here and the reader wants relatively more of it.
    # `channelIntakeOverride` lets a scarce, high-value source deliver more per run
    # without raising the global limit (which would let the bulk research feeds flood
    # the corpus again).
    overrides = cfg.get("channelIntakeOverride") or {}
    caps = {cid: int(overrides.get(cid, per_channel) or per_channel) for cid in wanted}

    def task(cid):
        t = time.time()
        attempts: list = []
        n_cap = caps.get(cid, per_channel)
        try:
            rows = COLLECTORS[cid](cfg, log, n_cap) or []
            primary_err = None
        except Exception as e:
            rows, primary_err = [], f"{type(e).__name__}: {e}"

        backup_used = None
        if not rows:
            for bid in (CHANNEL_SPECS.get(cid, {}).get("backup") or []):
                if bid not in COLLECTORS:
                    attempts.append({"id": bid, "result": "not_implemented"})
                    continue
                log(f"    {cid}: 主源无结果，尝试备用源 {bid}", "warn")
                try:
                    alt = COLLECTORS[bid](cfg, log, caps.get(bid, per_channel)) or []
                except Exception as e:                              # noqa: BLE001
                    attempts.append({"id": bid, "result": f"error: {type(e).__name__}: {e}"})
                    continue
                if alt:
                    rows = alt
                    backup_used = bid
                    attempts.append({"id": bid, "result": "ok", "count": len(alt)})
                    log(f"    {cid}: 备用源 {bid} 生效，取得 {len(alt)} 条", "ok")
                    break
                attempts.append({"id": bid, "result": "empty"})

        return cid, rows, primary_err, time.time() - t, backup_used, attempts

    with futures.ThreadPoolExecutor(max_workers=min(6, max(1, len(wanted)))) as pool:
        futs = {pool.submit(task, cid): cid for cid in wanted}
        for fut in futures.as_completed(futs):
            cid = futs[fut]
            try:
                cid, rows, err, dur, backup_used, attempts = fut.result()
            except Exception as e:                                  # noqa: BLE001
                rows, err, dur, backup_used, attempts = [], f"{type(e).__name__}: {e}", 0.0, None, []
            spec = CHANNEL_SPECS.get(cid, {"tier": "P2"})
            if err is None and rows:
                status = "ok"
            elif rows:
                # Primary failed but a backup produced items: still a success for
                # the reader, yet the log must show the degradation.
                status = "ok"
            elif err is None:
                status = "empty"
            else:
                status = classify_failure(err)
            results[cid] = build_channel_result(cid, spec, status, len(rows), err, dur,
                                                backup_used=backup_used, backup_attempts=attempts)
            label = CHANNEL_NAMES.get(cid, cid)
            via = f" [via {backup_used}]" if backup_used else ""
            if status == "ok":
                log(f"{label:<16} {len(rows):>4} items ({dur:.1f}s){via}", "ok")
            elif status == "empty":
                log(f"{label:<16} empty (host answered, no items)", "warn")
            elif status == "parse_error":
                log(f"{label:<16} PARSE ERROR (shape changed, not an empty feed): {err}", "err")
            elif status == "blocked":
                log(f"{label:<16} blocked: {err}", "err")
            else:
                log(f"{label:<16} {err}", "warn")
            raw_items += rows
    return results, raw_items


def run_probe(args) -> int:
    """--probe: one pass over the channels, print a health table, write nothing.

    WHY separate from --dry-run: --dry-run still normalizes, scores and dedupes
    the whole corpus and prints a run record; when you only want to know which
    upstream moved, that is wasted work and a lot of output. For the deep,
    evidence-carrying probe (candidate endpoints, robots.txt, regex diagnosis)
    use scripts/probe-channels.py.
    """
    log = Logger(args.verbose)
    cfg = load_config(CONFIG_PATH, log)
    if args.limit:
        cfg["limits"]["perChannel"] = args.limit
    per_channel = int(args.limit or 3)

    if args.only:
        wanted = [c.strip() for c in args.only.split(",") if c.strip() in COLLECTORS]
    else:
        wanted = [c for c, s in CHANNEL_SPECS.items() if s["mode"] != "manual"]
        wanted = [c for c in wanted if c in COLLECTORS and c not in DISABLED_CHANNELS]

    log(f"probe: {len(wanted)} channels, limit={per_channel}, nothing will be written", "step")
    results, _raw = collect_channels(wanted, cfg, per_channel, log)

    rows = []
    for cid in sorted(DISABLED_CHANNELS) if not args.only else []:
        spec = CHANNEL_SPECS.get(cid, {"tier": "P2"})
        rows.append(build_channel_result(cid, spec, "blocked", 0, None, 0.0, DISABLED_CHANNELS[cid]))
    rows += [results[c] for c in wanted if c in results]

    safe_print("")
    safe_print(f"{'channel':<18}{'status':<13}{'items':>6}{'sec':>7}  first item / error")
    safe_print("-" * 116)
    for r in sorted(rows, key=lambda x: (x["status"] != "ok", x["id"])):
        extra = r.get("error") or r.get("riskNote") or ""
        safe_print(f"{r['id']:<18}{r['status']:<13}{r['count']:>6}{r['durationSec']:>7}  {str(extra)[:62]}")
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    safe_print("")
    safe_print(f"probe summary: {counts}")
    safe_print(f"disabled (not attempted): {', '.join(sorted(DISABLED_CHANNELS))}")
    safe_print("deeper evidence: python scripts/probe-channels.py")
    return 0


def enrich_missing_summaries(items, cfg, log, cap=90, budget_sec=120, state=None):
    """Fetch a page's meta description for items that arrived with no summary.

    Why this exists: a card whose entire content is a title plus a link cannot be
    explained, scored, or deep-read - which is exactly the complaint that some
    cards have "只有摘要或链接". Two real causes were measured on 2026-10-01:

      * `huggingface.co/blog/feed.xml` carries NO content element at all: its
        entries are only title/pubDate/link/guid (verified across 870 entries),
        so all 23 HuggingFace blog cards had an empty summary.
      * Semantic Scholar's bulk endpoint returns `abstract: null` for a large
        share of records (14 of 37 s2 cards), and the landing page is then the
        only accessible place the abstract exists.

    Bounded three ways. The unbounded first version made a daily run take
    24 minutes (measured: 1455s, dominated by waiting on dead URLs), which is
    unacceptable for a scheduled task, so:
      * `cap`        - at most this many fetches per run
      * `budget_sec` - stop once this much wall time is spent, whatever the cap
      * `state`      - remember URLs that already failed and stop retrying after
                       3 attempts. Several sources (old Nowcoder discussion pages,
                       dead DOIs) genuinely have no meta description, and
                       re-attempting them every day is pure waste.

    Failures persist in `state["summaryFetchFails"]` so the next run skips them.
    Anything still summary-less is treated as low importance by prune_items.py
    rather than quietly passing as knowledge.
    """
    targets = [it for it in items if len(str(it.get("summary") or "").strip()) < 80]
    if not targets:
        return 0, 0
    fails = (state or {}).get("summaryFetchFails") or {}
    started = time.time()
    tried = 0
    filled = 0
    gave_up = 0
    for it in targets:
        if tried >= cap:
            break
        if time.time() - started > budget_sec:
            log(f"  summary backfill: stopped at the {budget_sec}s budget "
                f"(filled {filled} of {tried} attempted)", "warn")
            break
        url = str(it.get("url") or "")
        if not url.startswith("http"):
            continue
        if int(fails.get(url, 0)) >= 3:
            gave_up += 1
            continue
        tried += 1
        try:
            _s, body, _ct = http_get(url, timeout=8, cfg=cfg, log=lambda *a, **k: None,
                                     retries=0, accept="text/html,*/*;q=0.8")
        except Exception:
            if state is not None:
                fails[url] = int(fails.get(url, 0)) + 1
            continue
        if not body:
            if state is not None:
                fails[url] = int(fails.get(url, 0)) + 1
            continue
        # http_get may hand back bytes; the regexes below need str.
        if isinstance(body, (bytes, bytearray)):
            try:
                body = body.decode("utf-8", "replace")
            except Exception:
                body = str(body)
        head = body[:200000]
        desc = ""
        for pat in (
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']{40,})["\']',
            r'<meta[^>]+content=["\']([^"\']{40,})["\'][^>]+property=["\']og:description["\']',
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{40,})["\']',
            r'<meta[^>]+content=["\']([^"\']{40,})["\'][^>]+name=["\']description["\']',
            r'<meta[^>]+name=["\']citation_abstract["\'][^>]+content=["\']([^"\']{40,})["\']',
        ):
            m = re.search(pat, head, re.I | re.S)
            if m:
                desc = m.group(1)
                break
        if not desc:
            m = re.search(
                r'<p[^>]*class=["\'][^"\']*(?:abstract|summary)[^"\']*["\'][^>]*>(.{60,1200}?)</p>',
                head, re.I | re.S)
            if m:
                desc = m.group(1)
        desc = clean_text(desc, 900)
        if len(desc) >= 60:
            it["summary"] = desc
            it["summarySource"] = "page-meta-description"
            filled += 1
        elif state is not None:
            fails[url] = int(fails.get(url, 0)) + 1
        time.sleep(0.15)
    if gave_up:
        log(f"  summary backfill: skipped {gave_up} url(s) that already failed 3+ times", "info")
    if state is not None:
        state["summaryFetchFails"] = fails
    return filled, tried


def compose_summary(fresh, all_items, results, cfg, prev_summary=None) -> dict:
    """Build the day's digest summary — a deterministic "TL;DR" for humans.

    Deliberately mechanical: it selects and orders what is already in the data
    (highest relevance, most-reported categories, which channels delivered) and
    states counts. It does not paraphrase abstracts and it never asserts
    anything that is not derivable from the stored fields, so it stays honest
    even when the optional LLM layer is switched off. The agent layer can
    overwrite `headline`/`highlights` with richer prose afterwards.

    `prev_summary` matters for the `--rescore` path, which has no run results to
    report: without it the channel lists would be silently emptied and the
    headline would read "0/0 channels produced items".
    """
    prev_summary = prev_summary or {}
    top = sorted(fresh or [], key=lambda x: -(x.get("relevanceScore") or 0))[:6]

    # Diversify the "look at this first" list.
    #
    # Observed problem: a raw relevance sort put 3 job listings in the top 3 for
    # the day, crowding out the technical content the reader actually studies.
    # Job posts have high keyword match AND freshness, so they win that race.
    # Taking at most one item per category (then filling from the remainder)
    # keeps the summary about the day's breadth instead of one noisy channel.
    diversified: list = []
    seen_cats: set = set()
    for it in (fresh or []):
        cat = it.get("category")
        if cat in seen_cats:
            continue
        if len(diversified) >= 5:
            break
        diversified.append(it)
        seen_cats.add(cat)
    picked_ids = {it.get("id") for it in diversified}
    for it in sorted(fresh or [], key=lambda x: -(x.get("relevanceScore") or 0)):
        if len(diversified) >= 6:
            break
        if it.get("id") not in picked_ids:
            diversified.append(it)
            picked_ids.add(it.get("id"))
    top = diversified[:6]
    cat_fresh: dict = {}
    for it in fresh:
        cat_fresh[it.get("category")] = cat_fresh.get(it.get("category"), 0) + 1
    cat_total: dict = {}
    for it in all_items:
        cat_total[it.get("category")] = cat_total.get(it.get("category"), 0) + 1
    zh = sum(1 for it in fresh if it.get("lang") == "zh")
    with_code = sum(1 for it in fresh if it.get("codeAvailable"))
    peer = sum(1 for it in fresh if it.get("peerReviewed"))
    ok_channels = sorted(r["id"] for r in results.values() if r.get("status") == "ok")
    bad_channels = sorted(f"{r['id']}({r['status']})" for r in results.values()
                          if r.get("status") not in ("ok",))
    if not results:
        # No run data (the --rescore path): carry the previous observation over
        # rather than reporting a misleading zero.
        ok_channels = list(prev_summary.get("channelsWithItems") or [])
        bad_channels = list(prev_summary.get("channelsWithoutItems") or [])

    top_cat = sorted(cat_fresh.items(), key=lambda kv: -kv[1])[:3]
    headline = (f"本轮新增 {len(fresh)} 条，累计 {len(all_items)} 条；"
                + (f"集中在 " + "、".join(f"{(cfg['categories'].get(c) or {}).get('zh', c)} {n} 条"
                                          for c, n in top_cat) if top_cat else "本轮无新增条目")
                + (f"；{len(ok_channels)}/{len(ok_channels) + len(bad_channels)} 个渠道有产出。"
                   if (ok_channels or bad_channels) else "。"))

    highlights = []
    if top:
        highlights.append("最高相关：" + "；".join(
            f"{it.get('title', '')[:52]}({it.get('relevanceScore')})" for it in top[:3]))
    if peer:
        highlights.append(f"其中 {peer} 条来自同行评审渠道（OpenReview/会议），可信度更高")
    if with_code:
        highlights.append(f"{with_code} 条疑似附带开源实现，适合挑一个动手复现")
    if zh:
        highlights.append(f"{zh} 条为中文来源，与国内求职语境更贴近（牛客/知乎/量子位/雷锋网）")
    if bad_channels:
        highlights.append("未产出渠道：" + "，".join(bad_channels[:8])
                          + ("…" if len(bad_channels) > 8 else ""))

    return {
        "headline": headline,
        "highlights": highlights,
        "topItemIds": [it.get("id") for it in top],
        "counts": {
            "fresh": len(fresh),
            "total": len(all_items),
            "zhSources": zh,
            "peerReviewed": peer,
            "codeAvailable": with_code,
        },
        "byCategoryFresh": cat_fresh,
        "byCategoryTotal": cat_total,
        "channelsWithItems": ok_channels,
        "channelsWithoutItems": bad_channels,
        "generatedBy": "collect.py deterministic summarizer",
    }


def run(args) -> int:
    t0 = time.time()
    log = Logger(args.verbose)
    started = now_cst()
    log(f"Future daily collector v{COLLECTOR_VERSION}", "step")
    log(f"run time (Asia/Shanghai): {iso(started)}")

    cfg = load_config(CONFIG_PATH, log)
    if args.limit:
        cfg["limits"]["perChannel"] = args.limit
    if not CONFIG_PATH.exists() and not args.dry_run:
        save_default_config(CONFIG_PATH)
        log(f"wrote default config template: {CONFIG_PATH}")

    if args.only:
        # A subset run is by definition a partial check, so mark it narrow unless
        # the caller explicitly asked for a full-replacement digest.
        args.narrow = True
        wanted = [c.strip() for c in args.only.split(",") if c.strip() in COLLECTORS]
        log("--only given: disabled channels run too, on purpose, for re-testing", "warn")
        log("--only also implies --narrow: today's digest keeps its previous items", "warn")
    else:
        wanted = [cid for cid, spec in CHANNEL_SPECS.items() if spec["mode"] != "manual"]
        wanted = [c for c in wanted if c in COLLECTORS]
        # Channels switched off by config (e.g. the login-channel inbox while it
        # is paused) are treated like blocked ones: reported, never requested.
        opt_out = [c for c in wanted
                   if c in DISABLED_CHANNELS or CHANNEL_SPECS[c].get("disabled")]
        if CHANNEL_SPECS.get("inbox", {}).get("disabled") and not cfg.get("enableInbox"):
            opt_out = sorted(set(opt_out) | {"inbox"})
        wanted = [c for c in wanted if c not in DISABLED_CHANNELS
                  and not CHANNEL_SPECS[c].get("disabled")]
        if CHANNEL_SPECS.get("inbox", {}).get("disabled") and not cfg.get("enableInbox"):
            wanted = [c for c in wanted if c != "inbox"]
        blocked = [c for c in opt_out if c in DISABLED_CHANNELS]
        off = [c for c in opt_out if c not in DISABLED_CHANNELS]
        if blocked:
            log(f"DISABLED_CHANNELS not attempted ({len(blocked)}): {', '.join(sorted(blocked))}", "warn")
        if off:
            log(f"switched off by config, not attempted ({len(off)}): {', '.join(sorted(off))}", "warn")

    log(f"channels to run ({len(wanted)}): {', '.join(wanted)}", "step")
    if MANUAL_ONLY:
        log(f"skipping login-walled channels (human import only): {', '.join(sorted(MANUAL_ONLY))}", "warn")

    per_channel = int((cfg.get("limits") or {}).get("perChannel", 30))
    results, raw_items = collect_channels(wanted, cfg, per_channel, log)

    # Blocked channels are REPORTED but never requested: the run log and
    # web/data/sources.json must show why a channel is dark, or the next reader
    # will "fix" it again from scratch.
    for cid in (DISABLED_CHANNELS if not args.only else {}):
        if cid in COLLECTORS:
            spec = CHANNEL_SPECS.get(cid, {"tier": "P2"})
            results[cid] = build_channel_result(cid, spec, "blocked", 0, None, 0.0,
                                                DISABLED_CHANNELS[cid])

    log(f"raw items: {len(raw_items)} -> normalizing / scoring / deduping", "step")

    # State is loaded HERE, before the summary pass, because the pass both reads
    # the canonical store (to find cards whose summary is missing) and records
    # failed URLs into it. Loading it later made the gap scan reference an
    # undefined name.
    state = read_json(STATE_PATH, {})
    state.setdefault("version", 1)

    # Fill in summaries for content-less items BEFORE scoring, so relevance is
    # computed on real text rather than on a bare title.
    #
    # Priority matters as much as the pass itself. With a plain first-N cap, the
    # 40 fetch slots went to whatever the feed happened to return first, while the
    # genuinely summary-less cards already in the library were never reached -
    # which is why a pass reporting "28/40 filled" still left 19 empty cards.
    # Canonical entries with a missing/empty summary are therefore put at the
    # FRONT of the queue, so every run makes progress on the real backlog.
    summary_gaps: list = []
    if not args.no_enrich:
        try:
            canon_for_gaps = state.get("canonical") or {}
            raw_ids = {r.get("id") for r in raw_items}
            for cid, entry in canon_for_gaps.items():
                if not isinstance(entry, dict):
                    continue
                if len(str(entry.get("summary") or "").strip()) >= 80:
                    continue
                # Reuse the raw item when the same entry is in this batch, so the
                # improved summary flows through the normal ingest path; otherwise
                # synthesise a minimal record whose url we can fetch.
                match = next((r for r in raw_items
                              if r.get("id") == cid
                              or title_key(r.get("title")) == title_key(entry.get("title"))), None)
                summary_gaps.append(match or {
                    "id": cid, "title": entry.get("title"), "url": entry.get("url"),
                    "summary": "", "sourceId": entry.get("channel"),
                })
            if summary_gaps:
                log(f"summary gaps: {len(summary_gaps)} library card(s) have no usable "
                    f"description; queued for page fetch first", "warn")
        except Exception as e:
            log(f"summary gap scan failed: {e}", "warn")

    if not args.no_enrich:
        try:
            filled, tried = enrich_missing_summaries(summary_gaps + raw_items, cfg, log,
                                                     state=state)
            if tried:
                log(f"summary backfill: {filled}/{tried} items got a description "
                    f"from their page's meta tag", "info" if filled else "warn")
        except Exception as e:
            log(f"summary backfill skipped: {e}", "warn")

    deduper = Deduper(state, cfg)

    # ------------------------------------------------------------------
    # Learning signals — close the feedback loop with the workbench UI.
    #
    # Stars, reading status and mastery live in the browser's localStorage, which
    # this process cannot read. Without a bridge, ranking can only ever use
    # keyword heuristics and never adapts to what the reader actually studies -
    # which is the difference between "a feed" and "a workbench that helps me".
    # The progress page therefore offers 「写出学习信号」, which writes
    # web/data/feedback.json; here we apply it as a small, capped multiplier.
    #
    # Deliberately gentle: the bonus is capped so a burst of stars on one topic
    # cannot drown out the other directions, and it never overrides the relevance
    # gate. It nudges, it does not steer.
    # ------------------------------------------------------------------
    feedback = read_json(DATA_DIR / "feedback.json", {}) or {}
    fb_cat = feedback.get("byCategory") or {}
    fb_chan = feedback.get("byChannel") or {}
    if fb_cat or fb_chan:
        top_cat = sorted(fb_cat.items(), key=lambda kv: -kv[1])[:5]
        log(f"learning signals: {feedback.get('starredCount', 0)} starred / "
            f"{len(fb_cat)} categories engaged (top: "
            f"{', '.join(f'{k}×{v}' for k, v in top_cat) or '—'})", "info")
    else:
        log("learning signals: none yet — use 「写出学习信号」on the progress page "
            "to let ranking follow your interests", "info")

    def signal_bonus(cat: str, chan: str) -> float:
        """A capped ±bonus from the reader's own behaviour (0 when no signals)."""
        if not (fb_cat or fb_chan):
            return 0.0
        max_cat = max(fb_cat.values()) if fb_cat else 1
        max_chan = max(fb_chan.values()) if fb_chan else 1
        b = 0.0
        if cat and cat in fb_cat:
            b += 8.0 * (fb_cat[cat] / max_cat)      # up to +8 points
        if chan and chan in fb_chan:
            b += 4.0 * (fb_chan[chan] / max_chan)   # up to +4 points
        return round(b, 2)

    seen_ids = set(state.get("seenIds") or [])
    first_seen = state.setdefault("firstSeen", {})
    last_seen = state.setdefault("lastSeen", {})
    canonical = state.setdefault("canonical", {})

    # ------------------------------------------------------------------
    # Reconcile previously-applied learning bonuses.
    #
    # A bonus is baked into relevanceScore when an item is imported, so an item that
    # received one keeps that score forever - including after the reader deletes
    # feedback.json or their interests change. Measured: removing the file left 5
    # items still boosted. That is a silent, permanent distortion of the ranking.
    #
    # Fix: when a stored item carries a learningSignalBonus, remove exactly that
    # amount and recompute the bonus for the current signals. Self-correcting, and
    # idempotent because the stored bonus is always subtracted first.
    # ------------------------------------------------------------------
    rebased = 0
    for cid, entry in canonical.items():
        if not isinstance(entry, dict):
            continue
        bd = entry.get("relevanceBreakdown")
        prev = 0.0
        if isinstance(bd, dict):
            try:
                prev = float(bd.get("learningSignalBonus") or 0)
            except (TypeError, ValueError):
                prev = 0.0
        if not prev:
            continue
        base = float(entry.get("relevanceScore") or 0) - prev
        now_bonus = signal_bonus(entry.get("category"), entry.get("channel"))
        entry["relevanceScore"] = round(max(0.0, min(100.0, base + now_bonus)), 2)
        if isinstance(bd, dict):
            if now_bonus:
                bd["learningSignalBonus"] = now_bonus
            else:
                bd.pop("learningSignalBonus", None)
        rebased += 1
    if rebased:
        log(f"learning signals: rebased {rebased} previously boosted item(s) "
            f"(removes stale interest bonuses)", "info")

    # ------------------------------------------------------------------
    # Blocklist: items a human (or prune_items.py / dedupe_deep.py) removed.
    #
    # Without this check the whole cleaning effort silently undid itself: the
    # pruner archived 165 items and recorded their ids in state["blockedIds"], but
    # nothing read that list, so the very next collection re-imported them and the
    # library jumped straight back from 554 to 763. Enforcing it here is what makes
    # the blocklist actually mean something.
    #
    # Matched on BOTH the stable id and the canonical URL, so a re-published item
    # that gets a new id is still recognised.
    # ------------------------------------------------------------------
    blocked_ids = set(state.get("blockedIds") or [])
    blocked_title_keys = set(state.get("blockedTitleKeys") or [])
    blocked_urls = {str(u).lower().rstrip("/") for u in (state.get("blockedUrls") or [])}

    fresh, updated, dup_count, dropped, blocked = [], [], 0, 0, 0
    unclassified_dropped = 0
    news_dropped = 0
    opinion_dropped = 0
    weak_dropped = 0
    offtopic_dropped = 0
    spam_dropped = 0
    min_rel = float((cfg.get("scoring") or {}).get("minRelevance", 0) or 0)
    # Default True: keep unclassified items out of the corpus (see the gate below).
    drop_unclassified = bool((cfg.get("limits") or {}).get("dropUnclassified", True))

    for raw in raw_items:
        spec = CHANNEL_SPECS.get(raw.get("sourceId"), {"tier": "P2"})
        item = normalize(raw, cfg, spec["tier"])
        if not item:
            dropped += 1
            continue
        # Apply the reader's own learning signals before the gates, and record the
        # adjustment in the breakdown so the change is auditable in the UI rather
        # than a mysterious score drift.
        bonus = signal_bonus(item.get("category"), item.get("channel"))
        if bonus:
            item["relevanceScore"] = round(min(100.0, float(item["relevanceScore"]) + bonus), 2)
            bd = item.setdefault("relevanceBreakdown", {})
            if isinstance(bd, dict):
                bd["learningSignalBonus"] = bonus
        # --- blocklist gate ---
        if blocked_ids and item.get("id") in blocked_ids:
            blocked += 1
            continue
        if blocked_title_keys and title_key(item.get("title")) in blocked_title_keys:
            blocked += 1
            continue
        if blocked_urls:
            cu = str(item.get("canonicalUrl") or item.get("url") or "").lower().rstrip("/")
            if cu and cu in blocked_urls:
                blocked += 1
                continue
        if item["relevanceScore"] < min_rel:
            dropped += 1
            continue
        # `unclassified` never enters the corpus.
        #
        # An item that matched no category keyword cannot serve any tracked direction,
        # and letting them in wasted part of the 120k-entry de-duplication budget on
        # content that is invisible anyway (build_all_items drops it from the output).
        # Dropping at ingest is what makes "we do not keep unclassified cards" true
        # rather than cosmetic. Counted separately so the log distinguishes
        # "not good enough" from "matched nothing".
        if drop_unclassified and item.get("category") == "unclassified":
            unclassified_dropped += 1
            continue
        # --- evidence gate: "related enough" is not the same as "worth keeping" ---
        #
        # The reader's complaint was that items entered merely because they looked
        # recent or loosely related. A score threshold alone cannot express that,
        # because the score mixes in freshness and source tier, so a recent P0 item
        # with one generic keyword still clears any reasonable bar. This gate asks the
        # question directly: is there ENOUGH evidence that this belongs in a
        # job-hunting study library?
        #
        # SCOPE, and why it matters: the gate only judges WEAK material. The knowledge
        # types that are substantive BY DEFINITION - interview write-ups, mechanism
        # explanations, algorithm problems, fundamentals, real frameworks - are never
        # rejected for matching few keywords. An earlier version applied the
        # single-generic-keyword rule to everything and threw away genuine 面经 such as
        # "面了一轮Agent岗，我把问过的问题整理成了文章" (its only keyword was `agent`).
        # Where the material is unspecific (`other`), weak evidence is all there is, so
        # that is where the rule belongs.
        ktype = item.get("knowledgeType") or "other"
        hits = item.get("categoryHits") or []
        SUBSTANTIVE = ("method", "interview", "coding", "fundamentals", "project", "job")
        if ktype not in SUBSTANTIVE:
            if not hits:
                # Nothing matched any tracked direction and the type is unknown: this
                # is the "latest/adjacent news" case the reader described.
                if ktype == "news":
                    news_dropped += 1
                elif ktype == "opinion":
                    opinion_dropped += 1
                else:
                    offtopic_dropped += 1
                continue
            if len(hits) == 1 and str(hits[0]).lower() in GENERIC_KEYWORDS:
                weak_dropped += 1
                continue
        if ktype == "opinion" and not weak_enough_ok(hits, cfg):
            opinion_dropped += 1
            continue
        # 灌水/卖课 gate: a "面经/八股汇总" that is really a course advertisement.
        spam = spam_reason(item)
        if spam:
            spam_dropped += 1
            log.detail(f"- [spam] {spam}: {str(item.get('title'))[:64]}")
            continue
        cid = deduper.lookup(item)
        if cid:
            dup_count += 1
            last_seen[cid] = iso(now_cst())
            if canonical.get(cid):
                canonical[cid] = merge_duplicate(canonical[cid], item)
                updated.append(canonical[cid])
            continue
        item["firstSeen"] = iso(now_cst())
        item["lastSeen"] = item["firstSeen"]
        item["learning"] = {"status": "unread", "starred": False, "mastery": 0}
        canonical[item["id"]] = item
        first_seen[item["id"]] = item["firstSeen"]
        last_seen[item["id"]] = item["lastSeen"]
        seen_ids.add(item["id"])
        deduper.register(item, item["id"])
        fresh.append(item)
        log.detail(f"+ [{item['category']:<13}] {item['relevanceScore']:>5} {item['title'][:70]}")

    fresh.sort(key=lambda x: -x["relevanceScore"])
    cap = int((cfg.get("limits") or {}).get("maxNewPerRun", 400))
    if len(fresh) > cap:
        fresh = fresh[:cap]

    # ------------------------------------------------------------------
    # Per-category intake cap.
    #
    # The reader asked for depth over volume: 2-10 deeply-analysed items per
    # direction per day beats 40 shallow ones, and a lopsided corpus is what made
    # 'agent' hold 24% of everything. Without a cap a single chatty channel or a
    # broad keyword set can push one category far ahead of the others, which both
    # drowns the digest and starves the categories that matter (multimodal /
    # post-training / world models).
    #
    # Only the FRESH intake is capped - the existing library is never truncated, so
    # nothing already curated is lost. Items over the cap stay in the corpus from a
    # previous run and simply do not grow further this run.
    # ------------------------------------------------------------------
    per_cat_cap = int((cfg.get("limits") or {}).get("perCategoryPerRun", 0) or 0)
    if per_cat_cap > 0:
        # The cap is per category, but the reader wants a specific MIX: interview
        # write-ups and algorithm problems matter more, and they decay fastest (a 2024
        # 面经 is nearly worthless for a 2026 application). `categoryIntakeWeight`
        # scales each category's ceiling, so the desired mix is expressed as data in
        # collector.config.json and can be retuned without touching this code.
        intake_w = cfg.get("categoryIntakeWeight") or {}
        caps = {}
        for cid in cfg["categories"]:
            w = float(intake_w.get(cid, 1.0) or 0.0)
            caps[cid] = int(round(per_cat_cap * w))
        kept, used, trimmed = [], {}, 0
        for it in fresh:
            c = it.get("category") or "trend"
            if used.get(c, 0) >= caps.get(c, per_cat_cap):
                trimmed += 1
                continue
            used[c] = used.get(c, 0) + 1
            kept.append(it)
        # Always report the caps, even when nothing was trimmed.
        #
        # WHY unconditional: a monitor reads this line to confirm the intake mix is
        # still being enforced. Logging only on a trim made the line's ABSENCE
        # ambiguous - "caps applied and nothing hit them" and "the cap code never ran"
        # looked identical in the log, so the daily check could not actually verify
        # anything. `DROPPED 0` is the useful, unambiguous form.
        over = sorted(((c, n) for c, n in used.items()
                       if n >= caps.get(c, per_cat_cap)), key=lambda kv: -kv[1])[:5]
        log(f"per-category intake caps {json.dumps(caps, ensure_ascii=False)}: "
            f"dropped {trimmed} item(s)"
            + (f"; busiest: {', '.join(f'{c}={n}' for c, n in over)}" if over else ""),
            "info")
        # The categories the reader explicitly prioritises, so it is visible whether
        # the interview/algorithm mix is actually being fed.
        for cid in ("coding", "job", "exam"):
            if caps.get(cid):
                log(f"  {cid}: {used.get(cid, 0)}/{caps[cid]} 条", "info")
        fresh = kept

    deduper.prune()
    state["seenIds"] = list(seen_ids)[-120000:]
    state["lastRunAt"] = iso(now_cst())
    state["byKey"], state["byUrl"], state["byTitle"] = deduper.by_key, deduper.by_url, deduper.by_title
    state["hashes"] = deduper.hashes

    log(f"deduped: new={len(fresh)} updated={len(updated)} duplicate_hits={dup_count} "
        f"dropped={dropped} blocklisted={blocked} unclassified={unclassified_dropped} "
        f"news={news_dropped} opinion={opinion_dropped} weakEvidence={weak_dropped} "
        f"offTopic={offtopic_dropped} spam={spam_dropped}", "ok")
    if blocked:
        log(f"  {blocked} item(s) skipped by the blocklist (removed earlier by "
            f"prune_items.py / dedupe_deep.py); they will not come back", "info")
    if unclassified_dropped:
        log(f"  {unclassified_dropped} item(s) matched NO category keyword and were not "
            f"stored (off-topic news cannot serve any direction)", "info")
    # Report what the evidence gate rejected, per knowledge type, so the effect of the
    # rule is visible in the log rather than something you have to infer from counts.
    rejected = {"news": news_dropped, "opinion": opinion_dropped,
                "weakEvidence": weak_dropped, "offTopic": offtopic_dropped,
                "spam": spam_dropped}
    if any(rejected.values()):
        log("  evidence gate rejected: " + ", ".join(
            f"{k}={v}" for k, v in rejected.items() if v), "info")
        log("    (news/opinion/单一通用关键词 未经深度证据支撑时不入库；"
            "若某类被大量拒绝，应检查关键词或知识类型规则，而不是放宽门槛)", "info")

    duration = round(time.time() - t0, 1)
    ok_channels = sum(1 for r in results.values() if r["status"] == "ok")
    status = "ok" if ok_channels == len(results) and results else ("partial" if ok_channels else "error")

    # Reconcile backfilled summaries into the canonical store.
    #
    # Why this is needed: enrich_missing_summaries() improves the RAW items, but
    # for an item that already exists the deduper takes the `merge_duplicate`
    # branch, which keeps whatever summary the canonical record already had - i.e.
    # still empty. Measured effect: the pass reported "28/40 filled" while the
    # library still showed 19 summary-less cards afterwards. Writing the improved
    # text into canonical (and refreshing lastSeen) is what makes it stick.
    backfilled = 0
    if not args.no_enrich:
        # Collect every improved summary in this run, then push it into the
        # canonical record. Note the canonical store keeps whatever it already
        # had when the deduper takes the merge_duplicate branch, so without this
        # step an improved summary is computed and then thrown away.
        improved = {}
        for src in (summary_gaps + raw_items):
            s = str(src.get("summary") or "").strip()
            if len(s) >= 80 and src.get("summarySource") == "page-meta-description":
                for key in (src.get("id"), title_key(src.get("title"))):
                    if key:
                        improved[key] = s
        for cid, entry in (canonical or {}).items():
            if not isinstance(entry, dict):
                continue
            cur = str(entry.get("summary") or "").strip()
            if len(cur) >= 80:
                continue
            cand = improved.get(cid) or improved.get(title_key(entry.get("title")))
            if cand and len(cand) > len(cur):
                entry["summary"] = cand
                entry["summarySource"] = "page-meta-description"
                backfilled += 1
        log(f"summary reconcile: {backfilled} existing card(s) updated in place "
            f"(improved candidates in this run: {len(improved)})", "ok" if backfilled else "info")

    run_record = {
        "startedAt": iso(started), "finishedAt": iso(now_cst()), "status": status,
        # Distinguish an automatic 20:00 run from a human/dev invocation. Without
        # this the run history is ambiguous: during development there were many
        # manual runs in one afternoon, which looks exactly like "the schedule is
        # firing repeatedly" when reading logs/runs.json.
        "trigger": getattr(args, "trigger", "manual"),
        "narrowRun": bool(getattr(args, "narrow", False)),
        "durationSec": duration, "channelsOk": ok_channels, "channelsTotal": len(results),
        "channelsBlocked": sum(1 for r in results.values() if r["status"] == "blocked"),
        "channelsEmpty": sum(1 for r in results.values() if r["status"] == "empty"),
        "channelsParseError": sum(1 for r in results.values() if r["status"] == "parse_error"),
        "newItems": len(fresh), "updatedItems": len(updated), "duplicates": dup_count,
        "rawItems": len(raw_items), "collectorVersion": COLLECTOR_VERSION,
        "itemsFile": f"digest/{date_key(started)}.json",
        "notes": "; ".join(f"{r['id']}={r['status']}" for r in results.values()
                           if r["status"] != "ok") or "all channels reported items",
        "blockedChannels": {r["id"]: r.get("riskNote") for r in results.values()
                            if r["status"] == "blocked"},
    }

    if args.dry_run:
        log("--dry-run: nothing written", "warn")
        safe_print(json.dumps({"run": run_record, "newSample": [i["title"] for i in fresh[:10]]},
                              ensure_ascii=False, indent=2))
        return 0

    day = date_key(started)
    all_items = build_all_items(state, fresh, cfg)
    stats = compute_stats(all_items)
    # A narrowed run (`--only a,b`) exists to test or re-check specific channels.
    #
    # It must not be mistaken for the day's research. Replacing digest/today.json
    # with a 2-item subset made the optional agent layer believe the day had
    # almost no content (observed: it fell back to "top 12 from the whole index"),
    # and it made the 每日更新流 render a subset. So a narrow run unions the
    # previous digest's items into today's, fresh items first.
    narrow = bool(getattr(args, "narrow", False))
    if narrow:
        prior = read_json(DIGEST_DIR / "today.json", None)
        prior_items = (prior or {}).get("items") or []
        seen_ids_digest = {i.get("id") for i in fresh}
        carried = [i for i in prior_items if i.get("id") not in seen_ids_digest]
        if carried:
            log(f"narrow run: carried {len(carried)} items forward from the previous digest "
                f"so the day is not reduced to this subset", "warn")
            fresh = fresh + carried

    summary = compose_summary(fresh, all_items, results, cfg)
    digest = {
        "date": day, "generatedAt": iso(now_cst()), "run": run_record, "stats": stats,
        "summary": summary,
        "narrowRun": narrow,
        "items": fresh,
        "updated": [{"id": u.get("id"), "title": u.get("title"), "sources": u.get("sources")}
                    for u in updated[:80]],
    }
    atomic_write_json(DIGEST_DIR / f"{day}.json", digest)
    atomic_write_json(DIGEST_DIR / "today.json", digest)

    index = {"generatedAt": iso(now_cst()), "count": len(all_items), "items": all_items}
    atomic_write_json(INDEX_DIR / "index.json", index)
    atomic_write_json(DATA_DIR / "items.json", index)

    # Next scheduled run, derived from the fixed 20:00 Asia/Shanghai cadence, so
    # the UI never has to hard-code it (and stays right if the schedule moves).
    now = now_cst()
    nxt = now.replace(hour=20, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)

    manifest = {
        "generatedAt": iso(now_cst()), "lastRunAt": iso(now_cst()), "date": day,
        "collectorVersion": COLLECTOR_VERSION, "channelsOk": run_record["channelsOk"],
        "channelsTotal": run_record["channelsTotal"], "newItems": len(fresh),
        # Count what is actually PUBLISHED. `all_items` is the stored corpus, which
        # still holds `unclassified` entries (kept in state so a later keyword change
        # can bring the good ones back), but the index drops them - so reporting the
        # stored count made selfcheck warn "manifest.totalItems=720 but index has 635".
        "totalItems": len(build_all_items(state, [], cfg)),
        "storedItems": len(all_items), "durationSec": duration,
        "targetDate": cfg.get("targetDate"), "targetLabel": cfg.get("targetLabel"),
        "status": status, "itemsFile": f"digest/{day}.json", "stats": stats,
        "summary": summary,
        "schedule": {
            "cron": "0 20 * * *",
            "timezone": "Asia/Shanghai",
            "nextRunAt": iso(nxt),
            "taskName": "Future-Workbench-Daily-20",
            "runner": "scripts/run-daily.ps1",
        },
        "health": {
            "channelsWithItems": len(summary["channelsWithItems"]),
            "channelsWithoutItems": len(summary["channelsWithoutItems"]),
            "zhShare": stats.get("zhShare"),
            "emptySummaryItems": sum(1 for i in all_items if not (i.get("summary") or "").strip()),
            "itemsMissingUrl": sum(1 for i in all_items if not i.get("url")),
        },
    }
    atomic_write_json(DATA_DIR / "manifest.json", manifest)

    runs = read_json(RUNS_PATH, [])
    if not isinstance(runs, list):
        runs = []
    runs.insert(0, run_record)
    atomic_write_json(RUNS_PATH, runs[:120])

    # Channels + taxonomy doc for the 采集与运行 / 知识分类 views. Categories are
    # merged with whatever the (possibly richer) research registry already has.
    existing_sources = read_json(DATA_DIR / "sources.json", {}) or {}
    tax_by_id = {c.get("id"): c for c in (existing_sources.get("categories") or []) if c.get("id")}
    merged_cats = []
    for cid, c in cfg["categories"].items():
        base = tax_by_id.get(cid, {})
        merged_cats.append({
            "id": cid,
            "nameZh": base.get("nameZh") or c["zh"],
            "nameEn": base.get("nameEn") or c["en"],
            "description": base.get("description") or c.get("desc", ""),
            "collectionGoal": base.get("collectionGoal") or c.get("goal", ""),
            "updateCadence": base.get("updateCadence") or "每日 20:00 (Asia/Shanghai)",
            "relevanceWeight": c.get("weight", 0.8),
            "keywordsZh": [k for k in c["keywords"] if re.search(r"[\u4e00-\u9fff]", k)][:12],
            "keywordsEn": [k for k in c["keywords"] if not re.search(r"[\u4e00-\u9fff]", k)][:18],
            "arxivCategories": c.get("arxiv", []),
        })
    for cid, base in tax_by_id.items():
        if cid not in {c["id"] for c in merged_cats}:
            merged_cats.append(base)

    sources_doc = dict(existing_sources)

    # MERGE the channel registry instead of replacing it.
    #
    # Why: a run with `--only arxiv,hf_papers` (or any subset) used to overwrite
    # the whole `channels` array, so ~90 previously known channel records vanished
    # from the 采集与运行 view. Now a channel that did not run this pass keeps its
    # previous record with `staleSince` set, and the response explicitly reports
    # how many were observed this time versus carried over.
    prior_channels = {
        c.get("id"): c for c in (existing_sources.get("channels") or [])
        if isinstance(c, dict) and c.get("id")
    }
    ran_ids = set(results.keys())
    # Start from previously known records (breadth), then let this run's observed
    # results overwrite them one by one.
    merged_channel_store: dict = {cid: dict(rec) for cid, rec in prior_channels.items()}
    for cid, rec in results.items():
        merged_channel_store[cid] = dict(rec)

    for cid, prior in prior_channels.items():
        if cid in ran_ids:
            continue
        # Carried over: keep the previous observation but make the staleness explicit.
        entry = merged_channel_store[cid]
        entry["notRunThisPass"] = True
        entry["staleSince"] = entry.get("lastChecked") or existing_sources.get("generatedAt")

    sources_doc.update({
        "generatedAt": iso(now_cst()),
        "collectorVersion": COLLECTOR_VERSION,
        "categories": merged_cats,
        "channels": sorted(merged_channel_store.values(),
                           key=lambda c: (STATE_RANK.get(str(c.get("status")), 9),
                                          TIER_RANK.get(str(c.get("tier")), 9),
                                          str(c.get("id")))),
        "channelsObservedThisRun": sorted(ran_ids),
        "channelsRanCount": len(ran_ids),
        "channelsKnownCount": len(merged_channel_store),
        "manualChannels": [
            {"id": cid, "nameZh": CHANNEL_NAMES.get(cid, cid), "authRequired": True, "mode": "manual",
             "howTo": "人工登录后导出 CSV/JSON 放入 web/data/inbox/，下次运行时自动合并。"}
            for cid in sorted(MANUAL_ONLY)
        ],
        "scoring": cfg.get("scoring"), "dedupe": cfg.get("dedupe"),
        "limits": cfg.get("limits"),
        # NOTE: deliberately NOT setting `lastRunAt` here. A stale `lastRunAt`
        # field can be inherited from dict(existing_sources) and would contradict
        # manifest.json; that field is owned by manifest only.
        "runCounts": {
            "ok": sum(1 for r in results.values() if r.get("status") == "ok"),
            "empty": sum(1 for r in results.values() if r.get("status") == "empty"),
            "error": sum(1 for r in results.values() if r.get("status") == "error"),
            "blocked": sum(1 for r in results.values() if r.get("status") == "blocked"),
            "parse_error": sum(1 for r in results.values() if r.get("status") == "parse_error"),
        },
    })
    # Remove any inherited stale runs list / lastRunAt so the UI reads one source.
    for stale_key in ("lastRunAt", "runs", "notes", "build"):
        sources_doc.pop(stale_key, None)
    atomic_write_json(DATA_DIR / "sources.json", sources_doc)

    # Deterministic self-iteration: write a proposal file every run so the loop
    # produces output even when the optional LLM deep-read layer is skipped.
    write_proposals(cfg, results, fresh, all_items, log)

    taxonomy = {"generatedAt": iso(now_cst()), "source": "collect.py", "categories": merged_cats}
    atomic_write_json(DATA_DIR / "taxonomy.json", taxonomy)
    atomic_write_json(STATE_PATH, state)

    log(f"wrote digest/{day}.json, items/index.json, manifest.json, sources.json, logs/runs.json", "ok")
    log(f"done in {duration}s | status={status} | new={len(fresh)} | total={len(all_items)}", "ok")
    return 0


# ============================================================================
# 10. CLI
# ============================================================================
def build_parser():
    p = argparse.ArgumentParser(
        prog="collect.py",
        description="Future workbench - daily public-source research collector (stdlib only)",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--only", help="comma-separated channel ids, e.g. arxiv,github,hf_papers")
    p.add_argument("--narrow", action="store_true",
                   help="treat this as a partial check: keep the previous digest items so the "
                        "day is not reduced to this subset (implied by --only)")
    p.add_argument("--trigger", default="manual",
                   choices=["scheduled", "manual", "debug"],
                   help="how this run was started; recorded in logs/runs.json so the history "
                        "distinguishes the 20:00 schedule from human runs")
    p.add_argument("--no-enrich", action="store_true",
                   help="skip the page-fetch pass that fills summaries for items whose feed "
                        "carried no content (faster, but leaves title-only cards)")
    p.add_argument("--limit", type=int, help="max items per channel (overrides config)")
    p.add_argument("--dry-run", action="store_true", help="print results, write nothing")
    p.add_argument("--probe", action="store_true",
                   help="one lightweight pass over the channels, print a health table, write nothing")
    p.add_argument("--init-config", action="store_true", help="write the config template and exit")
    p.add_argument("--rescore", action="store_true",
                   help="re-apply classification + scoring to every stored item, then exit "
                        "(use after tuning keyword weights or the scoring formula)")
    p.add_argument("-v", "--verbose", action="store_true", help="trace every accepted item")
    return p


def rescore_all(cfg, log) -> int:
    """Re-apply classification and scoring to every stored item.

    Needed because relevanceScore is computed at ingest time and stored. After
    tuning keyword weights (for example, lowering 岗位与招聘 so job listings stop
    dominating the daily summary) the existing corpus keeps its old scores until
    it is rescored. Category assignment is refreshed too, so a keyword change
    also reclassifies items.
    """
    state = read_json(STATE_PATH, {})
    canonical = state.get("canonical") or {}
    if not canonical:
        log("no stored items to rescore", "warn")
        return 1

    changed_cat = changed_score = 0
    before_top = sorted(canonical.values(), key=lambda x: -(x.get("relevanceScore") or 0))[:5]

    for item in canonical.values():
        spec = CHANNEL_SPECS.get(item.get("sourceId"), {"tier": "P2"})
        cat, hits = classify(item, cfg)
        if cat != item.get("category"):
            changed_cat += 1
        item["category"] = cat
        item["categoryHits"] = hits
        item["knowledgeType"] = classify_knowledge_type(item, cat)
        rel, breakdown = score_item(item, cfg, spec["tier"], hits)
        if abs(float(rel) - float(item.get("relevanceScore") or 0)) > 0.05:
            changed_score += 1
        item["relevanceScore"] = rel
        item["relevanceBreakdown"] = breakdown
        item["why"] = explain(item, hits, cfg)

    state["lastRescoreAt"] = iso(now_cst())
    atomic_write_json(STATE_PATH, state)

    # A keyword/weight change alters the taxonomy's derived fields, so regenerate it
    # here too - otherwise the taxonomy silently disagrees with the index after every
    # rescore (see write_taxonomy's docstring).
    write_taxonomy(cfg)

    all_items = build_all_items(state, [], cfg)
    atomic_write_json(INDEX_DIR / "index.json",
                      {"generatedAt": iso(now_cst()), "count": len(all_items), "items": all_items})
    atomic_write_json(DATA_DIR / "items.json",
                      {"generatedAt": iso(now_cst()), "count": len(all_items), "items": all_items})

    # Keep the digest's summary consistent with the new scores.
    digest_path = DIGEST_DIR / "today.json"
    digest = read_json(digest_path, None)
    if isinstance(digest, dict):
        fresh_ids = {i.get("id") for i in (digest.get("items") or [])}
        fresh = [canonical[i] for i in fresh_ids if i in canonical]
        digest["summary"] = compose_summary(fresh, all_items, {}, cfg,
                                            prev_summary=digest.get("summary"))
        digest["rescoredAt"] = iso(now_cst())
        atomic_write_json(digest_path, digest)
        day = digest.get("date")
        if day:
            atomic_write_json(DIGEST_DIR / f"{day}.json", digest)

    log(f"rescored {len(canonical)} items: {changed_cat} reclassified, "
        f"{changed_score} score changes", "ok")
    after_top = sorted(canonical.values(), key=lambda x: -(x.get("relevanceScore") or 0))[:5]
    log("top 5 before -> after:", "info")
    for b, a in zip(before_top, after_top):
        log(f"  {(b.get('category') or ''):<13} {b.get('relevanceScore')} -> "
            f"{(a.get('category') or ''):<13} {a.get('relevanceScore')}", "info")
        log(f"     {str(a.get('title'))[:70]}", "info")
    return 0


def write_taxonomy(cfg: dict) -> None:
    """(Re)generate web/data/taxonomy.json from the effective category config.

    WHY this is a function and not inlined: the taxonomy was only written by the full
    collect path, so `--rescore` (the command you run right after changing keywords or
    weights) left taxonomy.json stale. That is how a category added to
    DEFAULT_CATEGORIES appeared in items/index.json but not in the taxonomy, and
    selfcheck then reported "85 items use a category not in taxonomy.json" - a warning
    that was both true and entirely an artefact of a missing write.
    """
    existing_sources = read_json(DATA_DIR / "sources.json", {}) or {}
    tax_by_id = {c.get("id"): c for c in (existing_sources.get("categories") or [])
                 if c.get("id")}
    merged: list[dict] = []
    for cid, c in cfg["categories"].items():
        base = tax_by_id.get(cid, {})
        merged.append({
            "id": cid,
            "nameZh": base.get("nameZh") or c["zh"],
            "nameEn": base.get("nameEn") or c["en"],
            "description": base.get("description") or c.get("desc", ""),
            "collectionGoal": base.get("collectionGoal") or c.get("goal", ""),
            "updateCadence": base.get("updateCadence") or "每日 20:00 (Asia/Shanghai)",
            "relevanceWeight": c.get("weight", 0.8),
            "keywordsZh": [k for k in c["keywords"] if re.search(r"[\u4e00-\u9fff]", k)][:12],
            "keywordsEn": [k for k in c["keywords"] if not re.search(r"[\u4e00-\u9fff]", k)][:18],
            "arxivCategories": c.get("arxiv", []),
        })
    for cid, base in tax_by_id.items():
        if cid not in {c["id"] for c in merged}:
            merged.append(base)
    atomic_write_json(DATA_DIR / "taxonomy.json",
                      {"generatedAt": iso(now_cst()), "source": "collect.py",
                       "categories": merged})


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.init_config:
        save_default_config(CONFIG_PATH)
        print(f"wrote {CONFIG_PATH}")
        return 0
    try:
        if args.rescore:
            log = Logger(args.verbose)
            return rescore_all(load_config(CONFIG_PATH, log), log)
        return run_probe(args) if args.probe else run(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
