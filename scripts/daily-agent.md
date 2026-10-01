# {{DATE}} · Future 工作台每日深读任务

你正在为「Future · 求职学习工作台」执行每日常规深读。工作目录 `{{ROOT}}`。
确定性采集层已经跑完，原始数据已经落盘。你的工作是**在已有数据上做归纳、剖析与易读化**，
并把结果写回数据层。

---

## 0. 铁律（违反即视为任务失败）

1. **禁止编造。** 你写的每一条结论都必须能在 `web/data/items/index.json` 的标题/摘要里
   找到依据。不确定就写「待核实」，绝不猜。
2. **禁止编造 URL。** 只能使用数据里已有的 `url` / `sources[].url`。不要"推测"论文链接。
3. **不抓取需要登录的站点**（小红书 / BOSS直聘 / 拉勾 / 实习僧）。若需要其中内容，
   在提案文件里写明"需人工导入"，不要尝试自动化。
4. **不改前端代码。** 只写 `web/data/` 下的文件，且只写下面列出的文件。
5. **不减小已有数据。** 写 JSON 前先读旧文件，做合并而不是覆盖。
6. **UTF-8 无 BOM。** 用 Python 写文件（`json.dump(..., ensure_ascii=False)`），
   不要用 PowerShell 的 `Set-Content` / `>`（会写 BOM 并可能损坏中文）。

---

## 1. 读入

1. 读 `web/data/manifest.json`：拿到本轮 `newItems`、`status`、失败的渠道列表。
2. **读 `web/data/deep-read-plan.json` —— 这是你的工作清单，不要自己另挑条目。**
   它由 `scripts/plan_deep_read.py` 生成，已经做了两件事：
   - **质量闸门**：把求职吐槽、泛化提问、内推码广告、纯薪资讨论标为 `skimmed`，
     这些**不要**做深度解析（它们仍留在卡片库里可搜索、参与去重，只是不占深度解析名额）。
   - **配额分配**：按分类给出 2–10 条名额，并给出每条的打分与来源。
   只处理 `queue` 数组里的条目，**每一条都必须产出完整解析（含图解，见第 2 节）**。
   `queue` 为空时直接跳到第 5 步（只写运行备注）。
3. 读 `web/data/digest/today.json` 与 `web/data/items/index.json` 取这些条目的原文摘要。
4. 读 `config/collector.config.json` 了解关注方向与权重。
5. 读 `web/data/formulas.json` 已有的公式 id，避免重复造。

> 为什么改成「按清单做」而不是「自己挑最相关的 8–12 条」：
> 质量优先需要**可复核**的选择依据。清单是确定性生成的，你可以质疑它、人工改它，
> 下一轮还能对比「计划 vs 实际」来评估筛选规则好不好。

---

## 2. 深读：每条都必须有「图解」

**硬性要求：`queue` 里每一条都要产出 `diagram`。** 这是当前最大的缺口 ——
有些卡片有解析、有些只有摘要和链接。图解必须**自包含**：用内联 SVG 或结构化描述，
**不要**依赖外部图片（无法离线、无法版本控制、链接会烂）。

```jsonc
"diagram": {
  "kind": "flow | architecture | curve | matrix | timeline",
  "title": "图的中文标题（≤20 字）",
  "caption": "一句话说明这张图想让你记住什么",
  // 方式 A（优先）：内联 SVG。必须自包含：viewBox、无外部引用、
  // 颜色只用 currentColor / var(--accent) / var(--fg-1) / var(--line-1)
  "svg": "<svg viewBox='0 0 480 200' xmlns='http://www.w3.org/2000/svg'>...</svg>",
  // 方式 B：结构化描述，前端按 type 用 CSS 画。适合流程/对比/时间线
  "spec": {
    "type": "flow",
    "nodes": [{"label": "输入", "detail": "图像 patch"}, {"label": "编码器", "detail": "ViT-L"}],
    "edges": [{"from": 0, "to": 1, "label": "patch 化"}]
  },
  "alt": "给读屏软件的文字替代（必填，一行）"
}
```

**画图纪律**：
- SVG 里不要出现 `<image>`、外链 `<use>`、`style` 里的外部 `url()`。
- 宽高比控制 16:6 ~ 16:9，`viewBox` 留 12px 边距，字号 ≥ 13（手机上要看得到）。
- 颜色只用主题变量或 `currentColor`，六套主题都要能正常显示。
- `spec.type` 只能是 `flow` / `architecture` / `curve` / `matrix` / `timeline`。
- 画不出来**不要编**：宁可用 `spec` 画一个朴素流程，也不要塞一个自己都解释不了的复杂 SVG。

对清单里的每条，产出一个 `enrichment` 对象：

```json
{
  "id": "<与 index.json 中完全一致的 id>",
  "tldr": "一句话结论，≤60 字，中文，直接说它做了什么/结论是什么",
  "keyPoints": ["3-5 条要点，每条 ≤40 字，必须来自原文摘要"],
  "why": "为什么对『多模态/后训练/生成式/世界模型』方向求职者重要，≤80 字",
  "difficulty": "easy|medium|hard",
  "tags": ["3-6 个英文小写标签"],
  "entities": ["方法名/机构名/模型名，来自原文"],
  "diagram": { "kind": "...", "title": "...", "caption": "...", "svg": "...", "spec": {}, "alt": "..." },
  "concepts": [
    {
      "name": "抽象概念名，例如 Group Relative Policy Optimization",
      "readable": "用大白话解释，≤100 字，面向有深度学习基础但没读过这篇论文的人",
      "analogy": "一个生活类比，必须具体、可想象，不要用『就像优化一样』这类空话",
      "analogyBreaksDown": "这个类比在什么地方失效 —— 比类比本身更重要，逼你说出真实边界",
      "mechanism": "机制：关键设计选择与它解决了什么，≤120 字",
      "visual": "用纯文本描述一个可视化方案：画什么轴、什么形状、什么颜色代表什么",
      "prerequisites": ["读它之前需要会什么"],
      "selfTest": ["3 道自测题，能答对才算学会"],
      "formula": "如果该概念有核心公式，给出 LaTeX 与符号表；没有则留空字符串"
    }
  ],
  "formulas": [
    {
      "id": "f-<slug>",
      "name": "公式中文名（英文名）",
      "category": "multimodal|posttraining|worldmodel|generative|rl|agent|foundation|engineering",
      "latex": "LaTeX",
      "symbols": [["符号", "含义"]],
      "derivation": ["逐步推导，2-5 步，每步一句话说清动机"],
      "analogy": "生活类比",
      "pitfalls": ["面试易错点"],
      "code": "可运行的 PyTorch/Python 片段（若公式可代码化）",
      "sources": [{"title": "原文标题", "url": "必须来自数据里已有的 url"}]
    }
  ],
  "selfCheck": {
    "claims": ["你能从摘要中直接证实的断言，逐条列出"],
    "uncertain": ["你不确定、需要人工核实的地方，逐条列出；没有就写空数组"]
  }
}
```

**公式剖析必须四层齐全**：符号表 → 推导链条 → 生活类比 → 可运行代码 → 易错点。
这是工作台「公式剖析」页面的核心价值，缺层会被视为未完成。

---

## 3. 岗位信息合并

如果本轮 digest 里出现招聘类条目（`category == "job"`），或字节/腾讯/阿里等招聘接口
返回了新的算法实习岗位：

1. 读 `web/data/jobs.json`（已有人工整理 + 每日增量的岗位库）。
2. 把新岗位**追加**到 `jobs` 数组，字段与既有条目保持一致
   （`id, company, title, directions, cities, pay, duration, conversion, open, applyUrl, sources, relevance`）。
3. `id` 用 `slug(company)-slug(title)` 保证稳定，重复岗位不要重复插入。
4. `directions` 只能用这些取值：`multimodal, post-training, world-model, generative, rl, agent, infra, embodied`。
5. 岗位信息里任何数字（日薪、名额、截止时间）若无法从原文确认，写 `null` 并在 `notes` 里标"待核实"。

---

## 4. 提案（自我迭代）

写 `web/data/proposals/{{DATE}}.json`：

```json
{
  "date": "{{DATE}}",
  "generatedBy": "daily-agent",
  "channelHealth": [{"id": "openreview", "status": "blocked", "evidence": "HTTP 403", "suggestedAction": "改为人工导入或寻找替代源"}],
  "taxonomyProposals": [{"action": "add|split|merge|reweight", "category": "worldmodel", "reason": "...", "proposedKeywords": ["..."]}],
  "keywordProposals": [{"category": "posttraining", "add": ["..."], "remove": ["..."], "reason": "近 7 日本方向命中率偏低/噪音偏高"}],
  "newChannelProposals": [{"id": "...", "why": "...", "verifiedUrl": "...", "authRequired": false}],
  "noiseReport": {"overBroadKeywords": ["..."], "underCoveredCategories": ["..."]},
  "humanReviewQueue": ["需要我（人类）确认的事项，逐条写清楚要确认什么"]
}
```
所有提案都只是**建议**，绝不自动生效；它们在「采集与运行」页面显示，等人工确认。

---

## 5. 写回

用 Python 脚本（放在 `scripts/` 下，例如 `scripts/apply_enrichment.py`，可复用）完成：

1. **`web/data/enrichment.json`** — 形如 `{"generatedAt": "...", "byId": {"<item id>": {...enrichment...}}}`。
   读旧文件后合并（新值覆盖同 id 的旧值），不要丢历史。
2. **`web/data/formulas.json`** — `{"generatedAt": "...", "source": "seed+daily", "formulas": [...]}`。
   读旧文件，按 `id` 去重后追加本轮的新公式；保留所有已有公式。
3. **`web/data/jobs.json`** — 见第 3 步（增量合并）。
4. **`web/data/proposals/{{DATE}}.json`** — 见第 4 步。
5. **`web/data/logs/agent-{{DATE}}.json`** — 运行记录：
   `{"date","startedAt","finishedAt","status","enrichedCount","formulaCount","newJobs","notes","errors"}`。

写完后运行一次校验：

```bash
python -c "import json,glob;[json.load(open(f,encoding='utf-8')) for f in glob.glob('web/data/**/*.json',recursive=True)];print('all json ok')"
```

---

## 6. 报告

最后用中文输出一份简短汇报（≤200 字 + 列表）：

- 本轮深读了几条、新增几个公式、几个岗位；
- 数据层健康状况（哪些渠道失败、是否需要人工介入登录类渠道）；
- 你在 `selfCheck.uncertain` 里标记的、需要我确认的事项；
- 下一步建议（1–3 条，具体可执行）。

---

## 附：绝对不要做的事

- 不要把整篇论文摘要复制进 `tldr`（那是采集层已经做的事）。
- 不要生成"这条很重要，建议阅读"这类无信息量的句子。
- 不要为了凑数给低相关度条目写 enrichment。
- 不要把无法验证的内容写成确定语气。
- 不要在 `notes` 里写英文以外的花哨格式；工作台渲染的是纯文本/Markdown 子集。
