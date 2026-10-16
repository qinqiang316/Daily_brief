## 2026-10-16：合并执行与轻检定时任务

- 将每日08:30启动cron与独立5分钟验收cron合并为一个5分钟任务：08:30后启动、运行中脚本轮询、ready/failed时唤醒Hermes。
- 日常语义检查改为轻量抽查；脚本机械闸门、真实浏览器凭据、finalize与发布哈希校验继续保留。
- 新增 `scripts/dailybrief_cron_tick.py` 与Hermes虚拟环境wrapper，避免系统Python缺浏览器依赖导致验收失败；加入5项调度状态回归测试。

## 2026-10-16：混合检索与Hermes语义复核

- 脚本批量搜索与抓取原文打底，小g每车道单次选文并按需Websearch补漏；移除生产路径中的小g独立复核和复核后重跑。
- 脚本保留正文/日期/原文/hash机械核验；候选显式标记待Hermes复核，问题由Hermes直接修正或拒收，finalize阻止跳过该步骤。
- HTML继续由 `modules/controlled_html.py` 固定模板生成并验收，不调用小g制作。
- 更新V2验收cron提示与今日简报skill。相关回归测试待全量复验。

## 2026-10-15：定时检索链路修复并完成一次真实报告输出

- 启动轮纯脚本、验收轮全天每5分钟，空结果不唤醒模型；生产cron与wrapper已读回核验。
- 修复HTTP JSON-RPC/SSE解包、检索/正文子预算、排队请求过期、原始URL与去重键混用、独立会话身份和正文抽取问题。
- 成熟来源传递给国内外检索车道；脚本冻结原文快照，独立复核保留；固定HTML直接渲染，复用现有Playwright驱动。
- 保留旧browser凭据，修正后的新凭据与HTML/MD精确哈希绑定；主席正文复核证据随批次归档。
- 386项回归及真实390/1280离线Chrome验收通过；正式输出8篇正文与4条视野拓展。完整故障/恢复边界见docs/2026-10-15-流程修复.md。

## 2026-10-14 小o剩余阻断修复（历史交接状态）
- transport-only Hermes新会话接管作者/复核/MD/HTML；真实身份及接管原因归档，同锁/manifest，不为模型否决或源校验失败换模型。
- 预算配置集中到pipeline_budget：车道含全部轮次/接管总1200秒，runner52分钟+主席预留5分钟；取消/超时清理自身进程组。
- HTML固定受控模板，逐篇canonical全字段绑定；真实390/1280 Chrome可见性/目录/筛选/details/统计/无网络验收。
- 持久launch/work/poll/claim/ack及完整cron操作文件，不依赖长cron轮无限续接。生产/cron/skills均未更改。
- 验证结果和准确测试数见scratch/dailybrief-o-finish-result.md；推理/检索E2E均为明确离线夹具，不声称双CLI联网成功。

# DailyBrief 脚本变更记录

## 2026-10-14：agent 检索管线 + 统一 HTML 交付（小k实施）

- 新增 `scripts/prepare_brief.py`（秒级任务上下文：窗口/去重/偏好探索/run_id/批次目录，
  供 cron 预运行）与 `scripts/run_pipeline.py`（主管线：确定性来源 + 并行双路 CLI
  检索 + 合并准入 + 小g写作/HTML 草稿，不 finalize）。
- 新增 `modules/agent_lane.py`：国内=小k（kimi CLI）、国外=小g（agy CLI）双车道，
  作者+独立复核双会话、独立进程组与日志、超时杀进程组、修正后再复核（≤2 轮）、
  真实故障状态（非零退出/超时/缺产物/未复核），不回退历史池。
- 新增 `modules/html_report.py`：HTML 点赞区脚本重建（标记位替换）+ HTML 与 canonical
  MD 一致性校验（条目/URL/摘要/点赞映射/run_id/拒收污染/离线资源/移动端静态要求）。
- `modules/run_store.py`：manifest 增加 html 路径；`scripts/finalize_brief.py`：
  MD+HTML 双发布（同日拒覆盖、幂等、HTML 哈希入快照与登记，声明 delivery=html 的批次
  缺 HTML 拒绝发布；旧批次 MD-only 兼容）；`scripts/brief_record.py`：记录 html_sha256。
- `scripts/dailybrief.py` 增加 prepare/pipeline 子命令。
- 候选 JSON 新增 delivery/pipeline.lane_status/agent_review 字段（validate 兼容）。
- 旧 collect_brief 入口保留兼容；操作规范与建议 cron prompt 见 docs/agent-pipeline.md。
- 测试：新增 tests/test_agent_lane.py、test_html_report.py、test_pipeline.py；
  全量 235 例离线回归通过。

## 2026-10-10：批次隔离、交付凭据与视野拓展

- 每次采集独立 run_id 目录，保存初始候选、审核副本、manifest、最终简报和校验输入快照。
- 增加 run_agy 与 finalize 入口：批次写入锁、真实超时清理、双重校验、幂等发布、禁止覆盖同日已发布版。
- 校验使用捕获的输入快照；元数据绑定 run_id、路径和哈希；登记失败禁止交付。
- 接入美日欧中视野拓展来源及晚点日期证据，限定标题条目只能出现在视野拓展区。
- Hermes 任务提示词纳入 docs；190 项离线回归测试通过。

## 2026-10-08 — 流程加固（小k执行）

审查对象：`scripts/collect_brief.py`、`validate_brief.py`、`like_links.py`、`brief_record.py` 及 `modules/window.py`、`retrieve.py`、`filter.py`、`rank.py`。验收报告见 `docs/流程加固验收.md`，回归测试 `tests/`（51 例全绿）。

| 指标 | 数量 |
| --- | --- |
| 确认缺陷 | 6 类（HN 豁免正文/热议日冒充发布日且占满池、validator 跨日错配、缺窗口与正文校验、畸形反斜杠 URL 放行、采集无锁/累积超时、点赞区存在即跳过残留旧链接） |
| 已直接修复 | 6 类全部 |
| 实改文件 | modules/filter.py、modules/retrieve.py、modules/rank.py、scripts/collect_brief.py、scripts/validate_brief.py、scripts/like_links.py、scripts/brief_record.py、scripts/likes.py；新增 scripts/replay_candidates.py、tests/（6 文件 51 例）、docs/流程加固验收.md |

- 准入：所有来源统一要求正文 ≥500 字 + 日精度发布日期 + 窗口内，HN 不豁免；HN created_at 仅作 `hn_discussed_at`；窗口外/未知/月精度/未来日期一律拒收（含速览 leftover）
- 排序：轨交优先 → 日期降序 → HN 积分只在同质量同日内排序；单方向 ≤4（轨交除外）；探索保底生活/健康优先
- 采集：fcntl 锁、原子落盘、`--reuse-cache` 显式同日缓存复用、空池熔断不覆盖、总预算 480s + 有界并发 4 + 局部故障隔离、run_id 写入候选
- 校验：文件名日期严格配对（跨日错配 FAIL）、窗口/日精度/word_count 证据校验、run_id 配对、未验证 leftover 零容忍
- 点赞：`like_links.py` 幂等重建点赞区（旧链接不残留）；`brief_record.py` 跨日守卫 + 幂等 upsert + 原子写
- 回放：`scripts/replay_candidates.py 2026-10-07` 实测旧池 0/8 合格，`[REPLAY_REFUSED]` 明确拒绝，只读不改历史

## 2026-09-01 — 首次代码质量检查与修复（小g执行，Hermes复核）

检查对象：`collect_brief.py`、`validate_brief.py`、`like_links.py`、`add_like.py`

| 指标 | 数量 |
| --- | --- |
| 发现问题 | 10 |
| 已直接修复 | 10 |
| 建议后续优化 | 1（`modules/retrieve.py` 临时查询文件清理） |
| 不建议变动 | 1（`modules/rank.py` / `modules/filter.py` 水文过滤与 72h 窗口规则） |
| 遗留阻塞风险 | 0 |

### add_like.py
- 修复 `find_brief(None)` 崩溃：安全处理 `None` / 空串参数，无匹配或无参时回退到最新简报，根除 `TypeError`
- `parse_brief_refs` 限定在「## 参考资料」段内提取，避免正文误伤
- Docstring 返回值说明修正为 `(ok, msg, dup)`

### like_links.py
- 修复无参执行级联崩溃（依赖 `add_like.find_brief` 的修复），现可直接 `python3 like_links.py`
- `BRIEF_DIR` 由绝对路径改为动态相对路径，增强可移植性
- 增强文末追加点赞区时的换行与段落格式判断

### validate_brief.py
- 独立加入规则 2 强校验：简报 URL 命中历史去重集合（历史简报/去重文件）直接 FAIL，不再只挂在“不在候选池”分支下漏检
- 校验去重时动态排除当前简报自身，消除误报
- `source_leftovers` 校验收紧：仅对日期未验证条目要求“必须在速览节 + 标注日期未验证”
- 偏好命中标记正则升级为兼容半角/全角括号

### collect_brief.py
- `QUERIES_CN` / `QUERIES_INTL` 改为动态模板：`{month} / {year} / {year_month}` 随当前时间生成（`build_default_queries`），跨月跨年不再硬编码
- 修复 `pick_leftover_item` 中 `month_only` 过窗检查逻辑（month 早于窗口起点所在月即剔）
- 拆解压缩单行语句为规范 Python 格式

### 验证
- 模块导入：4 个脚本全部正常加载
- `validate_brief.py` 校验 2026-09-01 简报：PASS（0 项违规）
- `like_links.py` 及 `--check`：正常识别，退出码 0
- `add_like.py --stats`：正常输出（17 条点赞，偏好方向：科技）
- `collect_brief.py`：正常采集并输出候选

### 备注
- 小g报告中提到的 `dailybrief.py status` 当前不存在（`scripts/` 下无此文件），属报告描述瑕疵，不影响已修脚本

## 2026-09-01 — 执行模式调整（Hermes 配置）

- cron `d0971e033e6b`（今日简报）切换为「Hermes 触发 → 小g(agy) 执行」：采集脚本先行（`collect_brief.py`），LLM 写作 / `validate_brief.py` 校验 / `like_links.py` 点赞由小g完成
- 失败兜底：小g 第一次失败自动重试一次；两次均失败由 Hermes 直接完成生成本期
- 交付方式：`deliver=local + origin`，避免微信限流影响
