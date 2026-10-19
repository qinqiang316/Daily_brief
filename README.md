# Daily Brief · 信息茧房意识与突破系统

一个**适用于每个人**的信息检索推荐系统：它既能帮你**深入了解自己真正关心的核心喜好**，也能帮你**看见并主动打破信息茧房**——而不是无限喂养你已经认同的观点。

它每天从你可能一辈子都不会偶然刷到的信息源里，替你各挑选一批"值得读"的内容：一部分强化你的长期兴趣，一部分刻意把你推向茧房之外的角度。你只需每周点几次赞，系统就持续学习你的偏好，并在反方向不断给你抛出新的、对抗性的视角。

> **核心理念**：推荐系统不应只是"猜你喜欢"。真正负责任的推荐，是在**贴合偏好（放大）**与**突破茧房（对冲）**之间保持张力——让你既深耕，又不画地为牢。

---

## 目录结构

```
DailyBrief/
├── README.md                  # 本文件
├── IDEA.md                    # 项目愿景与设计理念
├── .env.example               # 环境变量模板（复制为 .env）
├── requirements.txt           # 依赖说明（核心脚本 stdlib only）
├── modules/                   # 核心逻辑模块（采集/过滤/排序/偏好）
│   ├── window.py              # 时间窗口计算（断档补采 + 72h 硬上限）
│   ├── retrieve.py            # 采集（HN + 多源搜索 + 正文抓取）
│   ├── filter.py              # 硬过滤（去重/字数/日期/AI 水文检测）
│   └── rank.py                # 排序 + 偏好方向/探索方向注入
├── scripts/                   # 运行脚本
│   ├── collect_brief.py       # 采集编排入口：窗口→去重→搜索→过滤→候选池
│   ├── validate_brief.py      # 强校验闸门（PASS 才投递）
│   ├── likes.py               # 点赞偏好模块：方向推断/样本门槛/偏好/数据驱动探索
│   ├── add_like.py            # 点赞 CLI：按序号/URL 写入点赞
│   ├── like_server.py         # 点赞 HTTP 服务：点链接即记录（按需启动）
│   ├── like_ctl.py            # 点赞服务控制：start/stop/status
│   ├── like_links.py          # 简报文末点赞区幂等重建（按当前参考资料整体重建）
│   └── replay_candidates.py   # 历史候选池离线回放（只读体检，不合格明确拒绝）
├── tests/                     # stdlib unittest 回归测试（mock 离线，python3 -m unittest discover -s tests）
├── docs/                      # 验收报告（流程加固验收.md 等）
├── data/                      # 运行时生成（已 gitignore）
│   ├── _dedup_urls.json       # 已推送 URL 去重集合（自动维护）
│   └── likes.json             # 点赞记录（用户偏好，驱动检索增强）
└── output/                    # 仅保留正式简报 MD/HTML
```

> `dailybrief-v2-combined.sh` → `dailybrief_cron_tick.py` → `pipeline_job.py` 是当前定时入口；`run_pipeline.py` 编排批次采集、车道审核、写作与验收准备，`collect_brief.py` 提供采集逻辑。

---

## 它的双重目标

### ① 深入理解你的核心喜好
每次你点赞，系统记录该文章的方向（AI/科技/商业/生活/健康/轨道交通）、关键词、话题。样本积累到阈值后，检索时**优先从你的偏好方向多挖内容**——让你越来越懂自己在读什么、在意什么。

### ② 看见并突破信息茧房
系统不满足于"喂你喜欢的"。它持续做三件事：
1. **识别你的茧房**：统计你的兴趣分布，定位你高度集中、很少接触莫方面的方向
2. **主动对冲**：把探索预算刻意投向你没在读的方向（跨界）与对立方立场（对抗视角）
3. **范式注入**：候选池为探索内容保底席位，校验闸门强制"本期必须有探索内容进简报"

**一句话**：主引擎照你说的方向挖，破茧引擎照你**没说的方向**扔过来。

---

## 工作流

### 批次隔离与限时暂存（2026-10-17）

正式交付仅写入 `output/Daily-Brief-{date}.md` 与对应 `.html`；`output/` 不放候选、批次目录、发布凭据、验证证据或锁文件。包括 `Monthly-Academic-Brief-*.md` 在内的简报交付文件保留。

每批的候选、原文快照、日志、提示词、浏览器验收与发布凭据放在 `~/.hermes/cache/scratch/dailybrief-artifacts/`，由现有单一 cron tick 清理。未结案的 queued/running/ready/failed/claimed 票据对应材料不清理；其余批次创建满48小时后删除。已ack票据满48小时清理。清理严格限定在专用临时目录，不触碰 `output/` 或目录外文件。

同日防重不依赖临时归档：`data/brief_records.json` 持久登记正式MD/HTML哈希，cron启动前核对登记和留存的简报文件；候选与发布凭据过期后仍能识别已发布版本。简报草稿与候选路径由批次 manifest 指定，均在上述临时区。

```bash
# 写作仍绑定精确候选、prompt 与本批MD路径
python3 scripts/run_agy.py '<本次候选路径>' '<本次prompt文件>' --timeout 1200
# 主席审核后 finalize；只把 MD/HTML 放进 output/
python3 scripts/finalize_brief.py '<本次简报路径>' '<本次候选路径>'
```

同批次 agy 与发布使用临时区锁；等待超时不代表进程结束，不能直接重试。finalize 完成校验、重建点赞区、复检、登记 MD/HTML 哈希并交付。过程材料最长保留48小时；用户附件只使用 output 中的正式 HTML，canonical MD 同样作为简报交付文档留存。

```
单一 cron tick ─▶ 清理满48小时的非活动临时材料
                        │
                        ▼
                  独立批次采集/小g选文与写作
                        │
                        ▼
                  脚本校验 → Hermes 轻检 → 受控 HTML/浏览器验收
                        │
                        ▼
                  finalize → output 仅留简报 MD/HTML
                        └────发布登记与文件哈希供同日防重
```

---

## 快速开始

### 1. 安装

核心脚本仅依赖 Python 3 标准库，无需第三方包。搜索通过可插拔的 CLI 完成（见"依赖"）。

```bash
git clone https://github.com/qinqiang316/Daily_brief.git
cd Daily_brief
python3 scripts/collect_brief.py          # 产出当日候选池
```

### 2. 写作与校验

```bash
# （LLM 读取 _candidates/*.json 撰写简报，此处为示意）
# 强校验闸门 —— 必跑
python3 scripts/validate_brief.py
```

### 3. 点赞（驱动偏好学习）

```bash
python3 scripts/like_ctl.py start                    # 启动点赞服务（空闲15分钟自动退出）
# 或文字指令（无需服务）：
python3 scripts/add_like.py --brief Daily-Brief-2026-08-19.md --num 3 5
python3 scripts/add_like.py --url https://xxx --title "标题" --direction 轨道交通
python3 scripts/add_like.py --list
python3 scripts/add_like.py --stats
```

点赞阈值（可配置）：**总点赞 ≥10 条 且 最热方向占比 ≥40%** → 才认定偏好方向并注入偏好查询；探索查询优先走数据驱动，样本不足时回退日期轮换。详见 `likes.py`。

---

## 防信息茧房（硬规则）

| 机制 | 实现 |
|------|------|
| 偏好不垄断 | 偏好方向最多 2 条定向查询（基础查询不变） |
| 数据驱动反方向突破 | 探索优先投空白/低关注方向；无空白时投茧房对立/陌生角度查询；样本不足回退日期轮换 |
| 探索保底进池 | 候选池为探索内容预留 ≤2 席（`MAX_EXPLORE_SEATS`），生活/健康方向优先；生活/健康有合格条目时各保底 ≥1 席（不限探索，计入主题与 HN 上限） |
| 探索必进简报 | validate 硬校验：候选有探索候选时，简报须收录 ≥1 篇 |
| 偏好封顶 | 深度总结区「（偏好命中）」条目 ≤8（`MAX_PREF_DEEP`） |
| 样本门槛 | 点赞 ≥10 且最热方向 ≥40% 才启用偏好优先（`MIN_LIKES_FOR_PREFERENCE` / `MIN_PREF_SHARE`） |

探索内容在简报标注 **`（今日探索）`**，偏好命中标注 **`（偏好命中）`**。

---

## 强校验闸门（P0）

写作后必跑，FAIL 即修正或放弃本期，禁止直接投递：

```bash
python3 scripts/validate_brief.py
```

校验项：① 简报 URL ⊆ 候选池；② 无历史复现；③ 日期未验证不得收录（含速览 leftover，出现即 FAIL）；④ 候选先生成后写简报；⑤ 探索条目必收；⑥ 偏好命中 ≤8；⑦ 简报与候选**按文件名日期严格配对**（跨日错配 FAIL），引用候选日期须日精度且落在候选窗口内、**真实 content 实测 ≥500 字**（word_count 元数据仅参考不得冒充证据）、必须带 `date_source` 日期来源（旧格式布尔位不能冒充验证）；⑧ 候选带 run_id 时简报**必须标注** `<!-- run_id: ... -->` 且一致。（本机点赞链接 127.0.0.1:8900 已忽略。）

采集端准入（2026-10-08 加固，所有来源同一标准、HN 不豁免）：正文 ≥500 字 + 发布日期证据精确到日 + 窗口内，缺一不入推荐池；日期证据链按 **JSON-LD → HTML meta → `<time datetime>` → 署名发布时间标记 → URL 日精度 → 标题日精度** 取证并记录 `date_source`/`date_evidence`/`date_precision`，普通正文第一处日期（常为事件时间）不得冒充发布时间，全部日期过合法日历校验；HN created_at 仅作热议日期，不充当发布日期；畸形 URL 拒收不静默修复；空池熔断不覆盖旧候选；采集有锁、原子落盘、`--reuse-cache` 显式同日缓存复用；正文抓取/存储上限 6000 字符（容下英文 500 词）；**生活/健康有合格条目时各保底 ≥1 席**（不限探索候选，计入主题与 HN 上限）；所有网络阶段共享一个 480s 墙钟截止时间，搜索阶段用派生子预算为正文阶段预留 150s，偏好/轨交/探索方向查询先行，查询临时文件走 tempfile 可指定目录且 finally 清理（单次 socket 超时钳制于剩余预算，属预算化约束而非数学严格硬截止）。历史候选池可用 `python3 scripts/replay_candidates.py <日期>` 离线回放体检。

---

## 依赖

| 依赖 | 说明 |
|------|------|
| Python 3 | 核心脚本仅标准库，无第三方包 |
| 搜索后端 | 可插拔 CLI（本仓默认 anysearch skill），脚本通过子进程调用，替换搜索后端只需改一处调用 |
| 时间窗口 | 断档补采 + 72h 硬上限，防陈旧内容混入 |

---

## 路线图

- [x] 采集脚本化（去重 / 候选池 / 硬过滤 / 强校验全脚本化）
- [x] 点赞偏好 + 防信息茧房（偏好/探索轮换、硬校验）
- [x] 核心逻辑模块化（modules/：window/retrieve/filter/rank）
- [ ] **新源发现**：跳出既有固定源，发现新增量信息源及其高质量文章（新增量方向）
- [ ] **细粒度偏好画像**：从 6 大方向细化到关键词/话题/学科级
- [ ] **反方向拓展**：基于兴趣分布，刻意推荐对抗性/陌生角度内容
- [x] 统一 CLI 入口（scripts/dailybrief.py，包含 collect/validate/finalize）

详见 [IDEA.md](IDEA.md) 了解完整设计理念与演进方向。

---

## 许可

本项目为个人研究/实验用途，欢迎借鉴思路。
