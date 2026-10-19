# DailyBrief 管线说明

**2026-10-16现行版本**：每日简报的执行与轻检已合并为一个5分钟调度任务；08:30后启动一次，运行期间脚本轮询，只有 ready/failed 票据唤醒模型。语义检查采用轻量方式，机械准入、校验和发布哈希闸门保留。HTML由固定模板生成。当前流程与验收见本节及 [2026-10-15-流程修复.md](2026-10-15-流程修复.md)。

以下保留昨日交接稿用于追溯；其中“未修改cron”“模型生成HTML”等旧状态和预算分配描述不再代表部署现状。

## 昨日交接稿（历史）

当前是未提交工作区，未修改 cron、Hermes 全局配置/skills，未重跑今天简报。
正式入口必须从旧 collect 切到下述 launcher；仅更新技能不能完成切换。

## 执行链

`prepare_brief.prepare → run_pipeline → 脚本并行批搜/抓正文 → 并行cn/intl小g单次选文/按需Websearch补漏 → 脚本证据核验 → 合并准入 → 小g写MD → canonical校验/点赞 → controlled_html脚本生成HTML → 真实浏览器 → Hermes语义复核/直接修正或拒收 → finalize`。

小k固定 `kimi --model kimi-code/kimi-for-coding --output-format text -p`，不是K3。
小g使用 agy，未显式报告模型的原生执行记录写 unreported，不猜模型/provider。
国内/国外共享prepare固化的window/run_id/preference/explore和增强查询。
每车道只调用一次小g作者会话；脚本核对原文快照、正文≥500、日精度日期、窗口、正文一致性/hash、历史去重。产物语义复核状态明确为 `pending_hermes`；ready后由Hermes独立复核，问题直接修正或拒收，不回调小g。

临时产物（工作目录、候选、agent日志、发布凭据、浏览器证据）存放于 `~/.hermes/cache/scratch/dailybrief-artifacts/`，由同一cron tick清理：批次满48小时后删除；queued/running/ready/failed/claimed批次跳过。发布登记的MD/HTML哈希保存在 `data/brief_records.json`，过期材料不影响当日已发布核验。`output/` 只保留正式简报MD/HTML。

## 故障接管

`modules/executor.py` 在二进制缺失、非零、额度、鉴权、超时、缺产物时，同一步骤原进程组退出后最多启动一次Hermes。
调用真实CLI：`hermes --ignore-rules --toolsets web,terminal --usage-file <本步骤日志>.usage.json -z <原始完整任务>`。
不传model/provider、不续接历史session，使用启动时当前可用配置路由；实际身份以Hermes runtime usage的model/provider/session_id为准，不以配置默认值冒充。
本机2026-10-14 `hermes chat --help`、`hermes --help`已核验支持相应参数；本机runtime `hermes_cli/oneshot.py` 的usage字段也已逐项核对。
当前非秘密配置default=gpt-6-luna/provider=openai-codex；无工具、ignore-rules短探针返回DAILYBRIEF_PROBE_OK，model=gpt-6-luna。这不是检索/写作/真实接管成功证明。
官方依据：[CLI](https://hermes-agent.nousresearch.com/docs/user-guide/cli)、[CLI reference](https://hermes-agent.nousresearch.com/docs/reference/cli-commands)。

检索作者由采集锁覆盖；MD写作由run_agy批次writer锁覆盖；HTML由受控模板脚本生成，无HTML模型会话。
缺产物接管前删除失败/旧产物，禁止借旧文件成功。非法JSON、run_id错配、源校验/模型复核否决不是换模型触发器。
Hermes语义复核发现问题时直接改稿或调用拒收入口并重跑机械校验/HTML校验；禁止再次调用小g修正。Hermes transport接管仍只用于小g会话真实故障，不用于内容否决。
接管reason、原角色、executor、实际model/provider/session、逐次日志/usage/attempts落盘，保存在专用临时区，最多48小时。usage缺身份/未completed/failed/partial/interrupted不得成功。

## 可计算预算

配置唯一来源 `modules/pipeline_budget.py`；运行 `PYTHONDONTWRITEBYTECODE=1 python3 -m modules.pipeline_budget`。

| 阶段 | 总秒数 | 包含 |
|---|---:|---|
| 确定性来源 | 300 | 共用deadline |
| 并行cn/intl | 1200 | **每车道总额**，一次作者会话+真实故障接管+源重抓；并行墙钟只算一次 |
| 合并/补正文 | 180 | 新独立子预算 |
| canonical MD | 720 | 小g写作+Hermes共用总额 |
| HTML | 600 | 受控脚本生成+浏览器验收，无模型HTML会话 |
| 校验 | 120 | MD机械校验/点赞、HTML一致性、Chrome（单次最多110s），预留本地开销 |
| 主席/final | 300 | 新验收轮预算，不能被runner车道占用 |
| 活跃工作总额 | **3420=57分钟** | runner硬deadline **3120=52分钟**，预留主席5分钟 |

每车道时间用于脚本批搜、一次作者会话与真实故障接管；不再为模型复核/修正预留第二、第三个会话。
runner只接受不超过阶段上限的参数；总参数小于3420秒立即拒绝，不通过压缩准入或挤占后续阶段凑成功。
Python进程组管理，不依赖macOS GNU timeout；超时TERM→KILL并wait；正常退出也清理同组后裔。
runner SIGTERM/SIGINT关闭自身登记的CLI组；后台worker硬截止结束runner，失败持久化，不启动重试。
调度等待不计入活跃工作预算：5分钟poll间隔最多另等5分钟，因此正常完成交付审核的墙钟上限约62分钟（调度/系统延迟另计）；失败终止有最多10秒清理宽限。禁止再写“最坏50分钟/148分钟”。

## HTML交付及验收

模型HTML只保留为原始证据；发布HTML由 `modules/controlled_html.py` 从canonical及候选生成，CSS/JS固定，全部数据转义。
标记模板必须全文等于本地重建结果：任意inline CSS、class/祖先隐藏、屏外定位、transparent、0.0 opacity、脚本修改均拒绝。旧无模板HTML仅兼容两套明确测试样式，不能任意生成CSS/JS。
每篇完整摘要绑定标题、编号、原文URL；点赞由脚本绑定全部参考资料，编号999、重复、拒收URL（包括纯文本）均失败。
`verify_html_browser.py/.cjs` 使用自动发现的本机Chrome及独立临时profile，通过Playwright真实浏览器核每篇正文/祖先样式/字号、390/1280宽度、全部主题×地区筛选组合/统计、目录锚点及details展开。
禁止HTTP请求，无CDN，无用户browser/CDP复用，不监听8900。Node/Playwright/Chrome缺失或浏览器失败即停止，HTTP200不构成验收。
Chrome证据/截图/receipt保存在专用临时区，批次创建满48小时后清理；`output/`保留正式简报MD与HTML。

## 单一执行+轻检 Cron

项目 `scripts/pipeline_job.py` 继续负责launch/work/poll/claim/ack；当前只保留一个Hermes cron：

1. `dailybrief-v2-combined.sh` 每5分钟执行一次；每次 tick 先安全清理专用暂存区中已满48小时的非活动材料，再处理启动/发布状态。08:30前不启动。当日已登记发布时静默跳过。
2. 到08:30后若当日没有已尝试批次，脚本启动一次worker；运行中只轮询，不唤醒模型、不重复搜索。ready/failed票据才交给本轮Hermes处理。
3. ready票据互斥claim；Hermes做轻量语义检查：核对日期窗口、标题/引用与关键事实有无明显错配，不逐句做法证式复核。脚本机械校验、浏览器凭据、finalize哈希验证仍必须通过。
4. 通过后finalize并ack精确票据；失败只处理原批次，不自动重搜或另起同日批次。已发布的当日简报保持幂等跳过。
5. 运行入口使用 `~/.hermes/hermes-agent/venv/bin/python`，保证真实浏览器验收依赖可用。

实际单一cron使用 `~/.hermes/scripts/dailybrief-v2-combined.sh` 与 `docs/cron-v2-review-prompt.txt`；旧启动cron已移除，独立poll cron已并入该任务。`docs/cron-launch.sh`、`docs/cron-poll.sh`仅作历史模板，不再分别安装。

官方 [cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron) 有单轮时限；因此长流水线由独立worker运行，Cron短轮只启动/读状态，ready/failed时才进行轻检与发布。

## 小x复现（全部离线测试夹具）

```sh
cd '/Users/qqiang/AI project/05-日常工具/DailyBrief'
export TMPDIR=/Users/qqiang/.hermes/cache/scratch
export PYTHONDONTWRITEBYTECODE=1
python3 -m unittest discover -s tests -p 'test_*.py'
python3 tests/offline_pipeline_replay.py
python3 tests/offline_pipeline_replay.py --takeover
python3 -m modules.pipeline_budget
```

单测CLI/浏览器均用明确模拟adapter，含真实本地不存在二进制、timeout/取消进程组测试；两次独立E2E会真实启动独立headless Chrome。受沙箱限制Chrome可能需要应用启动权限，不能把该错误标通过。
E2E真实走run_agy接口/批次锁，全部新闻/推理为测试夹具；--takeover仅原二进制缺失为真实OS故障，Hermes推理/身份为SIMULATED。无真实kimi/agy检索，无受限kimi调用，不触动生产。

## 技能更新建议（本轮不改skills）

today-brief入口应改为launcher+独立poll票据；删除旧collect入口、后台notify无限等待、模型名称猜测、阶段各自1200/1500及“50分钟”条款。
保留全部准入/拒收/探索/编号点赞条款；HTML作为MEDIA交付，MD仅内部canonical；补受控模板、Chrome证据、Hermes transport-only接管与失败熔断、当天拒绝覆盖。
必须同步cron实际script/prompt与技能；旧入口仍collect时，新流程未启用。
