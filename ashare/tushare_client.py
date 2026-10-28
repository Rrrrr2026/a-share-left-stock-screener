#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tushare Pro (及协议兼容镜像) 原生 HTTP 客户端  ——  P0
======================================================
为什么不用 tushare SDK: ① SDK 钉死 pandas 版本, 本仓库两市共用一套依赖, 不接受它挑版本;
② SDK 自带的重试/限频看不见摸不着, 而本仓库的铁律是 **任何第三方网络调用必须有进程级墙钟期限**
(2026-09-03 三次挂死 + 一次线程版 OOM 的教训, 见 CHRONICLE)。所以直接 POST JSON:

    POST <base>  {"api_name": ..., "token": ..., "params": {...}, "fields": "..."}
    -> {"code": 0, "msg": "", "data": {"fields": [...], "items": [[...], ...]}}

token 与基址只从 `data/secrets.json` 的 `tushare_token` / `tushare_base_url` 读 (环境变量
TUSHARE_TOKEN / TUSHARE_BASE_URL 可覆盖, 用于临时切官方站)。**token 永不进日志/异常文本/git**。
基址走配置是因为老板买的是协议兼容镜像 (design/tushare_adapter_design.md §5), 可随时切回
api.tushare.pro。镜像站的基址是**完整 POST 地址** (2026-10-10 起 tl.kaixin8.top 带 /tushare/sdk 路径)。

备用基址 (2026-10-12 卡 TS-FALLBACK, 同日第二轮 TS-FALLBACK-2 加回切): 可选键 `tushare_base_url_backup` (环境变量
TUSHARE_BASE_URL_BACKUP 可覆盖) 配第二个站 (同一 key; 老板给的 datahubco 只有 15000 积分档, 权限可能少于主站)。
  · **切过去**: 主站连续 FAILOVER_AFTER=3 次传输层失败 (HTTP 非 200 且非 429 非 403 / 非 JSON / 连接错或 socket 超时; 跨调用、
    跨线程累计, 主站任何一次 JSON 应答即清零) 或连续 FAILOVER_AFTER_TIMEOUTS=6 次**进程级硬期限超时** (单独计, 阈值
    更高: 共享进程池被别的调用的超时 terminate 时, 在途的主站请求会一起被记成超时, 一次池重置顶多几条, 不够 6)
    或连续 FAILOVER_AFTER_FORBIDDEN=6 次网关 HTTP 403 **且覆盖 ≥ FAILOVER_FORBIDDEN_MIN_APIS=2 个不同接口** (单独计, 2026-10-28 卡 FAILOVER-2
    返工 1, 见下面「HTTP 403」一条: 同一接口 403 多少次都不切, 站级封禁才切)
    或返回 token 失效类错误 (code 2002 / 40101, 或文案 token+无效/过期) → 切到备用, 触发切换的那次调用在备用上重新
    来一轮完整重试; journal 一行 INFO「tushare 基址切换: 主站 <host> → 备用 <host> (原因: …)」(只打 host)。
  · **切回来**: 在备用上每 PROBE_EVERY_CALLS=20 次调用或 PROBE_EVERY_SEC=600 秒 (先到先试) 对主站打一次轻量 trade_cal
    (单日, 单次, socket 20s / 墙钟 30s), 应答 code 0 即回切并打一行 INFO「tushare 基址回切: 备用 <host> → 主站 <host> (原因: …)」;
    备用连续 FAILBACK_AFTER=3 次失败 (传输层 / 不认这把 key / **无权限或没这个接口**) 或连续 FAILBACK_AFTER_TIMEOUTS=6 次硬期限
    超时 (单独计, 与主站同口径) 也立即试探主站, 成功即回切并在主站重来一轮 —— 坏备用不会把主站的一次抖动放大成整轮停摆,
    15000 档缺的接口在主站回来后也能在主站补上; 两站都不可用就留在备用 (再满 3 次再试)。一次 query() 最多两轮
    (MAX_ROUNDS_PER_CALL), 防两站都抖时乒乓。**试探在自己的线程里跑, 不经 datasource 的共享进程池** (2026-10-17 卡
    TS-FALLBACK-3: 走池的试探撞 30s 期限会 terminate 整池, 连带在途的备用请求与 akshare 请求; 池被慢请求占满时试探还会在队列里
    等到超时而主站根本没收到请求 —— 校验员真进程池复现); 试探里抛了什么都只算「试探失败」, probing 位子在 finally 里释放。
  · **不计入切换的**: 业务错误 (无权限 40203 / unknown_api / 参数 / 超频) —— 它们是 JSON 应答, 反而清零主站计数; 网关 HTTP 429
    限流 —— 按超频线性退避重试, 不计也不清零; 网关 HTTP 403 —— 不进传输层计数, 单独计 (下一条)。15000 档把 JSON 权限错误当故障会循环切换,
    所以主站的 JSON 无权限不算、只有传输层才算; 主站的网关 HTTP 403 是唯一例外, 走下一条的独立计数 (连续 6 次且覆盖 ≥ 2 个接口才切)
    (备用上的无权限只触发「试探主站」, 不触发切换; 主站回不来照旧把 NoPermission 抛给调用方)。
  · **HTTP 403 = 该站该接口无权限, 不是站级传输故障** (2026-10-28 卡 FAILOVER-2; 当天 13:30 跑批 update.log 实录: 13:33 主站 3 次 daily Read timed out
    切到备用, 备用正常服务 daily / adj_factor / stock_basic, 13:37 却对 namechange 连回 3 次 HTTP 403 → 旧口径当传输层失败白重试 3 次 (2s/4s 退避) 并计满
    「备用连续 3 次失败」把全局状态踢回正在退化的主站, namechange 在主站又 3 次 Read timed out 才再切回备用, 月刷失败, 13:43 daily_basic 又在主站多烧
    3 次超时): `_post_json` 对 HTTP 403 抛 TushareNoPermission (消息「HTTP 403 (该站不提供此接口或无权限)」, api="http", code=403; 构造签名同
    TushareRateLimited, 能从 spawn 工作进程 pickle 回主进程, 线程回退路径同样成立); `_query_page` 把它接进与 JSON 业务码 NoPermission **完全相同**的
    分支 (`_permanent_failure`): 一次就抛、不进 3 次重试; 主站上**不计传输层失败也不清零** (答了 HTTP 403 不算「答了 JSON」, 主站连续传输层失败 /
    硬期限两个计数原样保留), 但进一个**独立的连续计数** `forbidden` (同日返工 1, 校验员: 站级 403 —— CDN/WAF 封 IP、网关按 key 封禁、镜像整站下线回 403 ——
    以前 3 次就切备用, 一刀切成「不计」会让成批调用 (daily / adj_factor / daily_basic 都不开 failover_once) 在主站被封时整轮全败而备用明明健康; 事实依据只有
    备用站对单一接口的一次观测, 不足以证明主站 403 也一定是按接口的): 连续满 FAILOVER_AFTER_FORBIDDEN=6 次 (与硬期限同阈值) **且这一串 403 覆盖 ≥
    FAILOVER_FORBIDDEN_MIN_APIS=2 个不同 api_name** 才切到备用并把本次调用在备用重来 (`_note_primary_forbidden`; 按接口的无权限只会卡在同一个接口上 ——
    同一接口 403 多少次都不切, 切过去另一站多半也没有、还会跟备用的 3 连败试探乒乓; 站级封禁会连着打到 daily / adj_factor / daily_basic), 主站任何 JSON
    应答照旧清零它 (`_note_primary_ok`), 回切也清, 切换时与别的计数一样不清; 切换行「tushare 基址切换: … (原因: 主站连续 N 次 HTTP 403 覆盖 K 个接口 (a, b),
    最后一次: <api> HTTP 403)」, 触发切换的那次调用打 WARNING「tushare <api> 在主站 HTTP 403 (<host>), 主站连续 403 已覆盖多个接口 = 站级封禁, 已切到备用,
    本次在备用重来」而不打「不重试」行; 备用上按卡 TS-FALLBACK-3 记一次备用失败 (不碰 forbidden), 满 3 次才试探主站, 主站回来了本次在主站重来, 否则把
    NoPermission 抛给调用方。日志一行 WARNING「tushare <api> 该站无权限 (HTTP 403, <host>), 不重试」(GM 按它 grep)。权限是**按站不同的** (主站能服务
    namechange 而备用不能, 反过来主站缺 income_vip), 所以「单次调用备用站重试」的可换站类型也把 TushareNoPermission (HTTP 403 或 JSON 无权限) 纳入 (见该条;
    那一轮 role None 不计 forbidden)。`--probe-backup` 另加 1 次信息性 namechange 备用直调 (403 → 打印「备用不提供 namechange (HTTP 403) — 月刷只能靠主站」,
    成功 → 打印行数; 不改退出码)。**没配备用键也一样**一次就抛 TushareNoPermission (不计数、不切换; 用例 test_primary_http403_without_backup_raises_once)
    —— 这是「有无备用都生效的三处刻意改动」的第三处 (下一条)。
  · **没配备用键**: 不计数、不切换、不试探, 错误分类顺序 (超频 → 参数 → 权限 → 传输) 与异常类型同 7b92bb6 版 (用例逐字节钉住);
    token 失效文案只在配了备用时才单独分出来 (TushareBadToken) 用于立即切换。**有无备用都生效的三处刻意改动** (前两处 2026-10-12, 第三处
    2026-10-28 卡 FAILOVER-2): 网关 HTTP 429 从「传输错误 2s/4s 退避抛 Transport」改成「超频 12s/24s 退避抛 RateLimited」; 「[unknown_api]」「[invalid_params]」
    及含「未开放」「参数缺失」的文案一次就抛 (NoPermission / BadParams), 不再白重试 3 次 —— 这两个子串比 kaixin8 实测的两种形态宽,
    是有意的 (都是永久性条件); 网关 HTTP 403 从「传输错误 2s/4s 退避重试 3 次抛 Transport」改成「一次就抛 TushareNoPermission」(见上面「HTTP 403」
    一条; 没配备用键时只是不重试, 不计数也不切换)。
  · **最坏墙钟**: 没备用 3×120 + 退避 2+4 ≈ 366s; 配了备用, 一次调用最多两轮 + 最多两次主站试探 (各 ≤ 30s):
    2×366 + 2×30 ≈ 792s ≈ 13 分钟 —— 有 TimeoutStartSec 的调用方按这个数收紧 retries / deadline_sec (见 trade_cal)。**分页的调用按页放大**
    (10-25 补): 每页都是一次完整调用, 一次 query() 的总墙钟上限 = 首页最坏 + (页数 − 1) × 单页最坏 (续页钉在同一站: 单页 = retries × deadline_sec + 退避,
    不再有第二轮与试探), 页数 ≤ PAGE_MAX=40; 全市场按日端点在备用上 2 页: 没备用机制 2×366 ≈ 732s, 配了备用 792 + 366 ≈ 1158s ≈ 19 分钟。
    按剩余预算算 deadline_sec 的调用方 (tools/tushare_ready_probe.py 一类) 要按「可能 2 页」留预算, 或传 paginate=False 只拿第一页。
  · **真验收**: `python -X utf8 -m ashare.tushare_client --probe-backup` (6 次调用: 主站 trade_cal + 两个开市日的 daily 走生产路径,
    备用站同三项直调绕开切换状态; 备用回包的 trade_date 必须**全部等于所问日** —— 生产按所问日期写价格库、不看回包日期, 无视或
    错配 trade_date 的备用站切过去就会静默写错 K 线和复权因子, 光数行数拦不住 (10-17 校验员实证); 第二个日子是已知的过去开市日,
    专抓「一律回最新一天」的镜像; 两天的代码集与主站重合 ≥ 90% 且行数 ≥ 主站 × 0.9), 退出码 0 可用 / 1 不可用 / 2 判不了 (另加 1 次**信息性**
    namechange 备用直调, 10-28 卡 FAILOVER-2: 只看备用有没有这个接口, 不改退出码);
    加键之前用环境变量 TUSHARE_BASE_URL_BACKUP 临时给备用地址先验 —— 主站健康时请求永远到不了备用站, 光跑 `pricestore ready`
    什么都验不出 (10-12 校验员实证)。
  · **单次调用备用站重试** (2026-10-27 卡 FAILOVER-1; 调用方 opt-in `query(..., failover_once=True)`): 上面的全局切换只在主站**连续** 3 次传输层失败 /
    6 次硬期限超时 / token 失效时才切, 一次 update 只打一次 Tushare 的阶段 (改名表月刷 namechange) 永远凑不齐 —— 10-26 / 10-27 服务器上 kaixin8 回
    JSON code=503 [upstream_pool_exhausted] (答了 JSON = 传输层正常, 计数反而清零), PC 上 3 次 120s 硬期限超时 (< 6), 三天都没碰过已配好的备用站。
    开了 failover_once 的调用: 在当前站 (按全局状态选的主站或备用) 照常跑完一轮 (retries × 重试 / 退避, 分页规则不变); 抛出的是 TushareTransport
    (传输层失败 / 硬期限超时 / _classify 落到 Transport 的瞬时业务错误码: 5xx 类、[upstream_pool_exhausted]、[minute_upstream_busy] 一类) 且**不是**
    TushareBadToken → 立即把**同一调用在另一站整体重来一次** (在主站就去备用, 在备用就去主站; 所有页钉在那一站, `_query_page` 的 pin=(站, None) 路径:
    不试探、不计数、不切换 —— 全局计数与当前站仍只由上面的规则管, 不双记也不翻转), 成功打一行 INFO「tushare 单次调用备用站重试: <api> 主站 <host> 失败
    (<原因>) → 备用 <host> 成功 N 行 (X.Xs)」; 两站都败打一行 WARNING (同前缀, 「也失败」) 并按**原错误类型**抛出, 消息带两站。**不触发**: 没配备用键
    (与今天完全一样, 连 try 都不进); 2002 / 40101 (TushareBadToken: 全局机制自己立即切); 429 / 超频 (只按现有退避等); 参数错 (永久性, 换站也一样);
    TushareTruncated (分页截断不是传输抖动, 语义不变); 本次调用已经由全局切换在两站各来过一轮 (再重来只是第三轮, 直接按原错误抛)。
    **无权限也换站** (2026-10-28 卡 FAILOVER-2, 见上面「HTTP 403」一条): TushareNoPermission (传输层 HTTP 403 或 JSON 无权限) 是**该站该接口**的永久性
    条件, 另一站未必 (10-28 实录主站能服务 namechange 而备用不能), 所以也纳入可换站类型, 两个方向对称 (在备用 → 单次去主站, 在主站 → 单次去备用),
    同样不改全局状态、不计数、不试探, 仍遵守「两站各来过一轮就不做第三轮」; 两站都失败按原错误类 (TushareNoPermission) 抛, 消息点名两站。非 opt-in 的
    调用行为不变 (主站 403 一次就抛; 备用 403 记一次备用失败后抛)。
    墙钟: 最坏再加一轮 (retries × deadline_sec + 退避; 备用上分页再按页放大), namechange 全表在备用 2 页: 366 + 2×366 ≈ 1098s。
  进程池里跑的 `_post_json` 只拿主进程选好的基址当参数, 切换状态只在主进程 (加锁) 判定。

分页 (2026-10-24 卡 TS-PAGINATE; 10-25 卡 TS-PAGINATE-2 按校验改口径): 备用站 datahubco **单页最多回 5000 行** (GM 10-24 用候选 secrets 跑 compat: 全市场按
trade_date 的 daily / adj_factor / daily_basic 都恰 5000 行; 加测 daily offset=5000 → 559 行 (合计 5559; 同日 09-29 主站 daily 回 5562 行 —— 两站行集差几行),
limit=6000 → 400「query limit exceeds the maximum」, daily_basic offset=5000 → 559 行); 主站 kaixin8 一页回全市场 5,56x–5,58x 行 (PC data/update.log
09-29..10-23: daily 5,561–5,572 / adj_factor 5,571–5,580 / daily_basic 5,559–5,572; 5,2xx 是 keep_a_code 剔北交所 / B 股后的 pricestore 行数) 所以从未暴露
—— 一旦切到备用, 不分页就静默丢 ~560 只。`query()` 对回包行数**恰等于本站单页上限** (`page_limit(base)`: secrets 键 `tushare_page_limit` / 环境变量
TUSHARE_PAGE_LIMIT, 整数 = 全局一个数, `{host: n}` 映射 = **按站配** (键是基址的 host, "*" = 其余站); 缺省 PAGE_LIMIT_DEFAULT=5000; ≤ 0 关掉) 的调用自动带
offset 续拉, 直到不足一页 / 0 行 / 服务端 has_more=False (回包里有这个字段就听它的), 各页合并后按该接口主键去重 (PAGE_KEYS; 没登记的接口有 ts_code+trade_date
就按它, 否则整行去重); 每一页都是一次完整的 `_query_page` (限频桶 / 硬期限 / 重试一样算), 多于一页时多打一行 INFO「tushare <api>: 分页 N 页 M 行 (<host>)」
(恰满一页而续页 0 行的不打)。行数 < 上限不翻页 —— 对本来就 ≤ 上限的接口零改动 (第一页的请求体与以前逐字节相同, 不带 offset / limit); 行数 > 上限也不翻
(本站上限其实更高, 回的已是全部)。调用方自己翻页的 (传了 offset / limit, 如 stock-core research/lake.py 的 `_pages`) 不自动翻; `paginate=False` 强关,
`paginate=True` 带 limit 也按本站上限翻。**一次 query() 的所有页钉在同一个站**: 第一页经切换机制选站, 续页只打第一页那个站 (不试探主站、不跟着切换 / 回切
过去 —— 两站行集与行序不同, 跨站按 offset 拼页会静默丢行, 校验员 10-24 E1/E2 实证), 失败计数照记, 切换 / 回切只在 query() 之间生效 (「每 20 次调用试探」
按 query() 数)。**截断一律抛 `TushareTruncated`** (带已拿到行数 / 页数 / 原因, 先打一行 WARNING「分页截断」), 不拼不同站的页也不返回截断表: 续页在钉住的站
失败耗尽 (或该站被判切换) / 镜像拒 offset (BadParams) / 镜像不认 offset (原样回上一页, 或续页去重后一行新的都没有) / **续页与已拿到的行重复 = 行序不稳**
(10-25 卡 TS-PAGINATE-3: PAGE_KEYS 登记了主键的接口, 续页里只要有一行的主键已出现过就判 —— 稳定的镜像不会把同一主键给两页; 没登记、按 ts_code+trade_date
或整行去重的接口, 去重后新增不足续页的一半才判。以前只认「一行新的都没有」: 认 offset 但每次排序不同的镜像, 续页大半与前页重复时静默返回并集, 少几百只
还过得了 pricestore 的 90% 守卫, 校验员 T1) / 续页行数 > 上限 (上限配置与本站不符) / PAGE_MAX 到顶; 调用方 (pricestore update / datasource daily_basic /
湖 pull_daily_basic / bulk) 都把异常当失败, 当天不写、下次再补。
`--probe-backup` 的备用直调 (`_direct_query`) 同样分页, 否则备用 5000/5559 = 89.9% 永远过不了 ≥ 90% 的门。

进程池回退 (同卡; 10-25 TS-PAGINATE-2 改宽限, 同日 TS-PAGINATE-3 改判据): `_deadline_call` 把 datasource 的进程级硬期限跑在一个看门线程里, **分两段计时、
各从该段开始时算**: 「取池」段 —— datasource 的池还没建成 (`_POOL is None`: 首次调用或上次超时 terminate 之后) 时, 工作线程先替它的执行器取池 (`ds._pool()`:
建池, 或等别的线程建完), 上限 = 它自己的建池预算 (`_POOL_DOA_WAIT_SEC + _POOL_PROBE_SEC` = 2 + 90s, 从 datasource 常量读, 不写死) + POOL_GRACE_SEC (30s);
「调用」段 —— 池已到手, 执行器提交并等结果, 上限 = deadline_sec + 30s (给提交 / 排队 / terminate)。上一版从调用开始统一按 deadline + 宽限计、到点只看
`_POOL is None`: 池在检查点之前刚建好、随后的调用又接近期限 (建池 45s 健康 + kaixin8 daily 110s) 照样被判挂死 → 永久回退线程版 + 置 datasource._POOL_DISABLED
+ 同一请求重发 (校验员 10-25 W1)。某段到点还没回来先做**二次探针** (`_pool_alive`): 往 datasource 当前的池直接投一个 os.getpid (1ms 的活; 不经 _call_with_deadline,
那条路超时会 terminate 整池), POOL_PING_SEC=10s 内回了 = 池机制活着、只是慢; 没回但工作进程都活着且任务占满 = 正忙; 池不在而「调用」段里有线程持着建池锁
= 正在重建 —— 这三种不回退, 再等一轮 (一次调用最多 POOL_EXTEND_MAX=1 轮: 正在重建 = 建池预算 + deadline_sec + 30s, 其余 deadline_sec + 30s)。**探针也不回**
(池对象拒收 / 不作答; 池不在且没有线程在建 = 刚 terminate 却不回来; 「取池」段给足建池预算后建池锁仍被持着) 或那一轮也用完 = 池机制挂住 (10-24 GM 在非交互 Bash
里跑 --probe-backup: spawn 报 PermissionError [WinError 5] 后 Pool.terminate 不回来, 挂到外层 300s 超时), 或从池机制抛出基础设施异常 (PermissionError /
BrokenPipeError / EOFError / BrokenProcessPool / datasource._PoolUnavailable; 工作进程里 fn 自己抛的异常带 RemoteTraceback, 不算) → WARNING 一行, 本进程余下
Tushare 调用永久改走线程版硬期限 (`_thread_call`, 期限仍生效), 并把 datasource._POOL_DISABLED 置 True (与 09-05 datasource「基础设施异常一次即永久回退线程版」
同口径, 让 akshare 那侧也别再碰可能已挂住的池)。池机制真挂时第一次调用多等: 建池挂住 92 + 30 ≈ 122s (上一版 deadline + 30 + 92 ≈ 242s); 池到手后 terminate 挂住
deadline_sec + 30s; 探针把挂住误看成「活着」时再多一轮。`--probe-backup` 开头先用一次 `_pool_ping` (POOL_WARMUP_SEC=20s) 把这条路走通, 池坏了在那里就回退
(建池挂住 ≈ 122s; 池建好后挂住 建池用时 + 20 + 30s), 后面 6 次不再各等一轮。

本模块提供:
    available()                      -> 是否配了 token
    page_limit(base=None)            -> 该站单页上限 (自动分页阈值: secrets tushare_page_limit 整数或 {host: n} / 环境变量同形 / 缺省 5000)
    credentials()                    -> (token, base_url, backup_base_url); 没配备用则第三项为 ""
    failover_state() / failover_reset() -> 备用基址切换状态 (诊断 / 用例用)
    probe_backup()                   -> 备用站真验收 (命令行 --probe-backup), 退出码 0/1/2
    pool_state()                     -> 进程池回退状态 (诊断 / 用例用)
    query(api, **params)             -> pandas DataFrame (空结果也带列名; 恰满一页自动分页, 见「分页」; failover_once=True 见「单次调用备用站重试」)
    index_daily(ts_code, start, end) -> 指数日线 DataFrame (升序)
    trade_cal(start, end)            -> 开市日列表 ["YYYY-MM-DD", ...]
    to_ts_code("sh000300")           -> "000300.SH"

设计要点:
  · 令牌桶限频 (默认 150 次/分钟, 低于镜像实测上限, 多线程共享)
  · 瞬时错误 (网络/5xx/非 JSON/超频/硬期限) 退避重试; **权限不足 / 必填参数缺失 不重试**
    (镜像的错误文本: "抱歉，您没有接口访问权限…" / "必填参数 标的" / "每分钟最多访问该接口N次";
     注意超频文本里也含"权限"二字, 所以分类顺序是 超频 -> 参数 -> 权限)
  · 每次 HTTP 经 `ashare.datasource` 的**进程级**硬期限执行 (懒导入, 避免与 datasource 循环)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

import pandas as pd

log = logging.getLogger("ashare.tushare")

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(PKG_DIR)
SECRETS_PATH = os.path.join(ROOT_DIR, "data", "secrets.json")

DEFAULT_BASE = "https://api.tushare.pro/"
RATE_PER_MIN = 150          # 令牌桶: 每分钟请求数上限 (镜像行情类实测 >200/min, 留余量)
TIMEOUT_SEC = 60            # 单次 socket 超时 (滴流时它不触发 -> 靠下面的硬期限)
DEADLINE_SEC = 120          # 进程级墙钟硬期限 (fina_indicator_vip 实测 21.9s, stk_factor_pro 13.5s)
MAX_RETRIES = 3
BACKOFF_SEC = 2.0           # 瞬时错误退避基数 (指数退避)
BACKOFF_RATE_SEC = 12.0     # 超频退避基数 (线性退避)
FAILOVER_AFTER = 3          # 主站连续这么多次传输层失败 (HTTP 非 200 非 429 非 403 / 非 JSON / 连接错) → 切到备用基址 (配了备用才计; 403 单独计, 见下面两行, 卡 FAILOVER-2)
FAILOVER_AFTER_TIMEOUTS = 6 # 主站连续这么多次进程级硬期限超时才切 (单独计, 阈值更高: 共享进程池被别人的超时重置会连带在途请求)
FAILOVER_AFTER_FORBIDDEN = 6    # 主站连续这么多次网关 HTTP 403 (单独计, 与硬期限同阈值) **且** 这一串 403 覆盖 ≥ FAILOVER_FORBIDDEN_MIN_APIS 个不同接口才切
                                # (2026-10-28 卡 FAILOVER-2 返工 1: 站级封禁 —— CDN/WAF 封 IP / 网关按 key 封 / 整站下线回 403 —— 会连着打到 daily / adj_factor / daily_basic)
FAILOVER_FORBIDDEN_MIN_APIS = 2 # 按接口的无权限 (10-28 实录 datahubco 对 namechange) 只会卡在同一个接口上: 同一接口 403 多少次都不切 (切过去另一站多半也没有, 还会乒乓)
FAILBACK_AFTER = 3          # 备用连续这么多次失败 (传输层 / 不认 key / 无权限) → 试探主站一次, 成功即回切 (坏备用不把主站一次抖动放大成整轮停摆)
FAILBACK_AFTER_TIMEOUTS = 6 # 备用连续这么多次进程级硬期限超时才试探 (单独计, 与主站同口径: 共享进程池被别人的超时重置会连带在途的备用请求)
PROBE_EVERY_CALLS = 20      # 在备用上每这么多次调用试探主站一次 (轻量 trade_cal)
PROBE_EVERY_SEC = 600       # …或每这么多秒 (先到先试)
PROBE_TIMEOUT_SEC = 20      # 试探那次的 socket 超时 (requests timeout; 滴流时它不触发, 靠下面的墙钟)
PROBE_DEADLINE_SEC = 30     # 试探那次的墙钟 (线程版 _probe_call: 到点就不等了, 不 terminate 任何东西, 不碰共享进程池)
PROBE_SECOND_DAY_BACK = 5   # --probe-backup 的第二个核对日: 最近开市日再往前这么多个开市日 (已知的过去开市日, 抓「一律回最新一天」的镜像)
MAX_ROUNDS_PER_CALL = 2     # 一次 query() 最多在两个基址上各来一轮 (切换/回切后重来一轮; 再切就不重来, 防两站都抖时乒乓)
TOKEN_ERROR_CODES = (2002, 40101)   # token 过期/无效类错误码 → 立即切备用 (10-08 旧镜像就是 code=2002 token已过期)
PAGE_LIMIT_DEFAULT = 5000   # 「本站单页上限」缺省 (备用站 datahubco 10-24 实测; 主站 kaixin8 一页回全市场 5,56x–5,58x 行, 恰 5000 行那天多打一页 0 行也无害);
                            # secrets 键 tushare_page_limit / 环境变量 TUSHARE_PAGE_LIMIT 可改: 整数 = 全局, {host: n} = 按站 (见 page_limit); ≤ 0 = 关掉自动分页
PAGE_LIMIT_ANY = "*"        # {host: n} 映射里「其余站」的键
PAGE_MAX = 40               # 自动分页最多翻这么多页 (40 × 5000 = 20 万行; 全市场按日端点 ≤ 6 千行, 单股全史 ≤ 6 千行), 防镜像每页都不同却翻不完
PAGE_KEYS = {               # 合并去重的主键 (列齐才用); 没登记的接口: 有 ts_code + trade_date 就按它, 否则整行去重
    "daily": ("ts_code", "trade_date"), "adj_factor": ("ts_code", "trade_date"), "daily_basic": ("ts_code", "trade_date"),
    "stk_factor": ("ts_code", "trade_date"), "stk_factor_pro": ("ts_code", "trade_date"), "moneyflow": ("ts_code", "trade_date"),
    "ths_daily": ("ts_code", "trade_date"), "index_daily": ("ts_code", "trade_date"), "sw_daily": ("ts_code", "trade_date"),
    "stock_basic": ("ts_code",), "trade_cal": ("exchange", "cal_date"),
}
POOL_GRACE_SEC = 30         # 看门线程每段健康上限之外再给池机制这么久 (提交 / 排队 / terminate): 「取池」段 = datasource 建池预算 (_pool_build_budget: DOA 2s + 就绪探针 90s,
                            # 从 datasource 常量读) + 它, 「调用」段 = deadline_sec + 它; 到点没回来先做二次探针 (_pool_alive), 探针也不回才判「池机制挂住」回退线程版
POOL_PING_SEC = 10          # 二次探针: 往池里直接投一个 os.getpid 最多等这么久 (健康的池 ~1ms; 工作进程都在忙时它排在后面 —— 那种看工作进程是否都活着、任务是否占满)
POOL_EXTEND_MAX = 1         # 二次探针说池没挂 (作答 / 正忙 / 正在重建) 时一次调用最多再等几轮; 用完仍不回 = 池机制挂住 (保证看门线程有界)
POOL_WARMUP_SEC = 20        # --probe-backup 开头的进程池热身 (_pool_ping) 期限: 健康的 spawn 池 5–8s 就绪 (2s DOA 等待 + 起解释器 + 导入); 池机制挂住时按 _deadline_call 的两段计时回退线程版

#: 测试用: 置 False 后在本进程内直调 (进程池里 mock 的 session 不生效)。生产恒为 True。
USE_PROCESS_DEADLINE = True


# ---------------------------------------------------------------- 异常
class TushareError(RuntimeError):
    """基类。构造签名保持 (message, api, code) 且 message 必给 —— 异常要能被
    multiprocessing 从工作进程 pickle 回主进程。"""

    def __init__(self, message: str, api: str = "", code=None):
        super().__init__(message)
        self.api = api
        self.code = code


class TushareUnavailable(TushareError):
    """没有配 token (或 secrets.json 缺失) —— 调用方应回退到旧数据源。"""


class TushareTransport(TushareError):
    """网络/HTTP/JSON 解析层错误 —— 可重试。"""


class TushareBadToken(TushareTransport):
    """token 过期/无效 (code 2002 / 40101, 或文案 token+无效/过期)。**只在配了备用基址时**才被 `_classify` 分出来
    (没配备用时判错顺序与 7b92bb6 版一字不差, 这类文案照旧落到 超频/参数/权限/传输 四类里); 仍是 TushareTransport 的
    子类, 同一基址上的重试语义不变, 只多一件事 —— 在主站遇到它就立即切备用 (各镜像各自校验 key)。"""


class TushareRateLimited(TushareError):
    """超频 ("每分钟最多访问该接口N次", 或网关层 HTTP 429) —— 线性退避后重试; 不计入备用基址切换。"""


class TushareNoPermission(TushareError):
    """积分/权限不足 —— 重试无意义, 直接抛给调用方去回退。JSON 业务码 (_classify) 与网关 **HTTP 403** (_post_json; 2026-10-28 卡 FAILOVER-2:
    备用站 datahubco 对 namechange 回 403 而主站能服务 —— 该站该接口的确定性无权限, 不是站级传输故障) 都是它; 构造签名 (message, api, code)
    不变, 403 那种 api="http", code=403, 能从 spawn 工作进程 pickle 回主进程 (与 TushareRateLimited 同)。权限按站不同, 所以 failover_once
    把它也纳入可换站类型 (_failover_once_eligible)。主站上的 HTTP 403 另进独立的连续计数 (同日返工 1: 连续 6 次且覆盖 ≥ 2 个不同接口 = 站级封禁才切备用,
    见 _note_primary_forbidden)。"""


class TushareBadParams(TushareError):
    """必填参数缺失/参数错误 (镜像对 forecast/express 要求带 ts_code) —— 不重试。"""


class TushareTruncated(TushareError):
    """分页截断 (2026-10-25 卡 TS-PAGINATE-2; 同日 TS-PAGINATE-3 补「行序不稳」): 第一页恰满本站单页上限, 续页却拿不全 —— 续页在钉住的站失败耗尽
    (或该站被判切换) / 镜像拒 offset / 镜像不认 offset (原样回上一页, 或续页去重后没有新行) / 续页与已拿到的行重复 (行序不稳: 登记了主键的接口任一重复,
    没登记的新增不足续页一半) / 续页行数 > 上限 / PAGE_MAX 到顶。**不返回截断表** (全市场按日端点少 ~560 只会被 pricestore 的
    90% 守卫放过去写死进库), 整次 query() 抛这个 (TushareError 的直接子类, 不是 Transport: 不该被当成「可重试的传输抖动」原地重试);
    rows / pages = 已拿到的行数 / 页数, reason = 原因 (都不含 token); 续页失败时 __cause__ 带原始异常。调用方 (pricestore update / datasource
    daily_basic / 湖 pull_daily_basic / bulk) 走各自现有的异常路径把它当失败: 当天不写、下次再补。"""

    def __init__(self, message: str, api: str = "", code=None, rows: int = 0, pages: int = 0, reason: str = ""):
        super().__init__(message, api, code)
        self.rows, self.pages, self.reason = int(rows), int(pages), str(reason)

    def __reduce__(self):
        return (type(self), (str(self), self.api, self.code, self.rows, self.pages, self.reason))


_RATE_HINTS = ("每分钟最多访问", "每小时最多访问", "抽取失败", "频率", "too many", "rate limit")
# 「[invalid_params] 业务参数缺失或格式不合法」(kaixin8 code 400): 'invalid param' 带空格对不上下划线形态, 10-13 卡 TS-PROBE 实测
_PARAM_HINTS = ("必填参数", "参数错误", "缺少参数", "不能为空", "invalid param", "invalid_params", "参数缺失")
# 「[unknown_api] 接口不存在或未开放」(kaixin8 code 404, 研究湖的 fina_indicator_vip / income_vip): 以前被当传输错误白重试 3 次
_PERM_HINTS = ("没有接口访问权限", "权限", "积分", "permission", "未开通", "unknown_api", "未开放")
_TOKEN_HINTS = ("无效", "过期", "invalid", "expired")     # 与 "token" 同时出现才算 (镜像各家文案不一)


def _is_token_error(code, low: str) -> bool:
    """token 失效类: 错误码 2002 / 40101 (字符串形态也认), 或文案同时含 token 与 无效/过期。"""
    try:
        if int(code) in TOKEN_ERROR_CODES:
            return True
    except (TypeError, ValueError):
        pass
    return "token" in low and any(h in low for h in _TOKEN_HINTS)


def _classify(api: str, code, msg: str, with_backup: bool = False) -> TushareError:
    """按镜像返回的错误码/文本判断该不该重试。

    顺序要紧: 超频文本里也含"权限"二字, 所以是 超频 -> 参数 -> 权限 -> 传输 —— 这个顺序和四种异常类型与 7b92bb6 版
    (没有备用基址机制的最后一版) 完全一样, 用例逐字节钉住。**只有 with_backup=True (配了备用基址) 时**才在最前面多判一步
    token 失效 (code 2002 / 40101 或文案 token+无效/过期 → TushareBadToken, 用于立即切备用): 没配备用时「token无效, 没有
    接口访问权限」照旧是 NoPermission 一次就抛, 不会变成重试 3 次 (10-12 校验员抓的「没配备用键一切照旧」说过头)。
    新认两种 kaixin8 文案 (10-13 卡 TS-PROBE): [unknown_api] → NoPermission, [invalid_params] → BadParams, 都不再重试。
    """
    m = str(msg or "")
    low = m.lower()
    if with_backup and _is_token_error(code, low):
        return TushareBadToken(f"{api}: token 过期/无效 code={code} ({m[:120]})", api, code)
    if any(h in m or h in low for h in _RATE_HINTS):
        return TushareRateLimited(f"{api}: 超频 ({m[:120]})", api, code)
    if any(h in m or h in low for h in _PARAM_HINTS):
        return TushareBadParams(f"{api}: 参数不合法 ({m[:120]})", api, code)
    if any(h in m or h in low for h in _PERM_HINTS):
        return TushareNoPermission(f"{api}: 无权限/积分不足 ({m[:120]})", api, code)
    return TushareTransport(f"{api}: code={code} {m[:120]}", api, code)


def _redact(text, token: str = "") -> str:
    """把 token 从任何要进日志/异常/屏幕的文本里抹掉 (镜像的 msg / 网关错误页偶尔会回显请求体)。先抹再截断。"""
    s = "" if text is None else str(text)
    if token and token in s:
        s = s.replace(token, "<token>")
    return s


# ---------------------------------------------------------------- 凭据
_CRED = None
_CRED_LOCK = threading.Lock()


def _read_secrets() -> dict:
    try:
        with open(SECRETS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:      # noqa: BLE001  (损坏的 secrets 不该炸掉整条流水线)
        log.warning("secrets.json 读取失败: %s", type(e).__name__)
        return {}


def credentials(refresh: bool = False):
    """-> (token, base_url, backup_base_url)。token 只在进程内存里流转, 永不打印。
    backup_base_url 没配就是 "" (secrets 键 tushare_base_url_backup / 环境变量 TUSHARE_BASE_URL_BACKUP);
    refresh=True 重读 secrets 并把基址切换状态复位 (新凭据从主站重新开始), 单页上限缓存 (page_limit) 也清掉重读。"""
    global _CRED, _PAGE_LIMIT
    if _CRED is None or refresh:
        with _CRED_LOCK:
            s = _read_secrets()
            token = (os.environ.get("TUSHARE_TOKEN") or s.get("tushare_token") or "").strip()
            base = (os.environ.get("TUSHARE_BASE_URL") or s.get("tushare_base_url")
                    or DEFAULT_BASE).strip()
            backup = (os.environ.get("TUSHARE_BASE_URL_BACKUP") or s.get("tushare_base_url_backup")
                      or "").strip()
            _CRED = (token, base or DEFAULT_BASE, backup)
        if refresh:
            failover_reset()
            _PAGE_LIMIT = None      # 新凭据可能换了站, 单页上限重读
    c = _CRED
    if len(c) == 2:             # 旧形态 (用例直接塞 (token, base)): 等价于没配备用
        c = (c[0], c[1], "")
    return c


def available() -> bool:
    """是否配了 token (调用方据此决定走 Tushare 还是旧源)。"""
    try:
        return bool(credentials()[0])
    except Exception:           # noqa: BLE001
        return False


_PAGE_LIMIT = None          # page_limit() 的缓存: int (全局一个数) 或 {host: int} (按站; 环境变量 / secrets / 缺省); credentials(refresh=True) 清掉重读


def _parse_page_limit(raw):
    """secrets 键 tushare_page_limit / 环境变量 TUSHARE_PAGE_LIMIT 的值 -> int (全局一个数) 或 {host: int} (按站)。
    整数或整数字串 = 全局; dict (secrets 里的 JSON 对象) 或以 "{" 开头的 JSON 字串 (环境变量) = 按站, 键是基址的 host (urlsplit 的 netloc, 如
    "datahubco.com"; 也认完整基址; "*" = 其余站), 值是该站的单页上限; 看不懂的按缺省 PAGE_LIMIT_DEFAULT 并 WARNING (映射里某一站看不懂只丢那一站)。"""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return PAGE_LIMIT_DEFAULT
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            raw = json.loads(raw)
        except ValueError:
            log.warning("tushare_page_limit 不是整数也不是 {host: n} 映射 (%r), 按缺省 %d", raw[:80], PAGE_LIMIT_DEFAULT)
            return PAGE_LIMIT_DEFAULT
    if isinstance(raw, dict):
        out = {}
        for k, v in raw.items():
            try:
                out[str(k).strip().lower()] = int(str(v).strip())
            except (TypeError, ValueError):
                log.warning("tushare_page_limit[%r] 不是整数 (%r), 该站按缺省 %d", k, v, PAGE_LIMIT_DEFAULT)
        return out
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        log.warning("tushare_page_limit 不是整数 (%r), 按缺省 %d", raw, PAGE_LIMIT_DEFAULT)
        return PAGE_LIMIT_DEFAULT


def _page_limit_config():
    """缓存的上限配置 (int 或 {host: int}): 环境变量 TUSHARE_PAGE_LIMIT > secrets 键 tushare_page_limit > 缺省。"""
    global _PAGE_LIMIT
    if _PAGE_LIMIT is None:
        raw = os.environ.get("TUSHARE_PAGE_LIMIT")
        if raw is None or str(raw).strip() == "":
            raw = _read_secrets().get("tushare_page_limit")
        _PAGE_LIMIT = _parse_page_limit(raw)
    return _PAGE_LIMIT


def page_limit(base=None) -> int:
    """`base` 这个站 (主站或备用的基址; 不传 = 不分站) 的单页上限 (行): 环境变量 TUSHARE_PAGE_LIMIT > secrets 键 tushare_page_limit > PAGE_LIMIT_DEFAULT;
    值是整数就全站一个数, 是 {host: n} 映射就按 `base` 的 host 取 (没列的站用 "*", 再没有用缺省); ≤ 0 = 该站关掉自动分页。
    回包行数**恰等于**它才续页 (见 _page_more) —— 所以这个数必须是**该站真实的**单页上限: 写大了 (站只给 5000 你写 6000) 等于关掉分页, 静默少拉;
    写小了 (站能给 5,5xx 你写 5000) 只是恰好 5000 行那天多打一页 0 行。两站上限不同就按站各写 (2026-10-25 卡 TS-PAGINATE-2; 以前只有一个全局数,
    主站真实上限 ≠ 全局数时主站被它自己的上限截断查不出来)。分页用的上限按**第一页实际打到的站**取 (_paginate)。"""
    cfg = _page_limit_config()
    if isinstance(cfg, dict):
        b = str(base or "").strip()
        for key in (b.lower(), _host(b).lower(), PAGE_LIMIT_ANY):
            if key and key in cfg:
                return cfg[key]
        return PAGE_LIMIT_DEFAULT
    try:
        return int(cfg)
    except (TypeError, ValueError):
        return PAGE_LIMIT_DEFAULT


# ---------------------------------------------------------------- 备用基址切换 (状态只在主进程, 加锁)
_FO_LOCK = threading.Lock()
#: 初始状态 (failover_reset 回到这里; 用例也拿它比):
#:   active 在备用上 / fails 主站连续传输层失败 / timeouts 主站连续硬期限超时 (单独计) / reason+switched_at 上次切换
#:   forbidden 主站连续网关 HTTP 403 (单独计, 卡 FAILOVER-2 返工 1) / forbidden_apis 这一串 403 打到过的接口名 (排好序的 tuple —— 不可变, 复制状态时不共享;
#:     连续满 FAILOVER_AFTER_FORBIDDEN 次且 len ≥ FAILOVER_FORBIDDEN_MIN_APIS 才切) /
#:   backup_fails 备用连续失败 (传输层 / 不认 key / 无权限) / backup_timeouts 备用连续硬期限超时 (单独计) /
#:   calls_on_backup 切换 (或上次试探) 以来在备用上的调用数 / last_probe_at 上次试探 (monotonic)
#:   probing 有线程正在试探 / probes·switches·returns 累计次数 (诊断)
_FO_INITIAL = {"active": False, "fails": 0, "timeouts": 0, "forbidden": 0, "forbidden_apis": (), "reason": "", "switched_at": None,
               "backup_fails": 0, "backup_timeouts": 0, "calls_on_backup": 0, "last_probe_at": None, "probing": False,
               "probes": 0, "switches": 0, "returns": 0}
_FO = dict(_FO_INITIAL)


def _host(url) -> str:
    """只取主机名: 日志/异常文本里基址只以 host 出现 (完整地址虽不含 token, 也没必要进 journal)。"""
    try:
        from urllib.parse import urlsplit
        return urlsplit(str(url)).netloc or str(url)
    except Exception:           # noqa: BLE001
        return str(url)


def _has_backup(base, backup) -> bool:
    return bool(backup) and backup != base


def _active_base(base: str, backup) -> tuple:
    """主进程判定本次请求打哪个基址 -> (base_url, role)。
    role None = 没配备用 (什么都不计, 行为与没有这套机制时完全一样); "primary" = 打主站且配了备用 (失败要计数、JSON 应答
    要清零、token 失效要切); "backup" = 已切到备用 (失败计 backup_fails, 连续 FAILBACK_AFTER 次就试探主站)。"""
    if not _has_backup(base, backup):
        return base, None
    with _FO_LOCK:
        if _FO["active"]:
            return backup, "backup"
    return base, "primary"


def _switch_locked(base: str, backup: str, reason: str) -> None:
    """调用方持 _FO_LOCK。只打 host; token 不在这里也不在 reason 里 (reason 来自抹过 token 的异常文本/错误码)。"""
    _FO.update(active=True, reason=reason, switched_at=time.time(), backup_fails=0, backup_timeouts=0, calls_on_backup=0,
               last_probe_at=time.monotonic())
    _FO["switches"] += 1
    log.info("tushare 基址切换: 主站 %s → 备用 %s (原因: %s)", _host(base), _host(backup), reason)


def _return_locked(base: str, backup: str, reason: str) -> None:
    """调用方持 _FO_LOCK。回到主站: 两边计数全部清零 (主站从头算), 打一行 INFO。"""
    on_backup = time.time() - (_FO["switched_at"] or time.time())
    n = _FO["calls_on_backup"]
    _FO.update(active=False, reason="", switched_at=None, fails=0, timeouts=0, forbidden=0, forbidden_apis=(),
               backup_fails=0, backup_timeouts=0, calls_on_backup=0, last_probe_at=None)
    _FO["returns"] += 1
    log.info("tushare 基址回切: 备用 %s → 主站 %s (原因: %s; 在备用 %d 次调用 / %.0f 秒)",
             _host(backup), _host(base), reason, n, on_backup)


def _note_primary_transport_failure(base: str, backup: str, reason: str) -> bool:
    """主站一次传输层失败 (HTTP 非 200 非 429 / 非 JSON / 连接错或 socket 超时)。连续满 FAILOVER_AFTER 次 → 切换并
    返回 True (本次调用随即在备用上重来一轮); 已经切过 (本线程或别的线程) → False, 不再计数。"""
    with _FO_LOCK:
        if _FO["active"]:
            return False
        _FO["fails"] += 1
        if _FO["fails"] < FAILOVER_AFTER:
            return False
        _switch_locked(base, backup, f"主站连续 {_FO['fails']} 次传输层失败, 最后一次: {reason}")
        return True


def _note_primary_timeout(base: str, backup: str, reason: str) -> bool:
    """主站一次进程级硬期限超时。**单独计, 阈值更高** (FAILOVER_AFTER_TIMEOUTS): datasource 的 spawn 池是 akshare 与
    Tushare 共用的, 任何调用撞硬期限都会 terminate 整池, 在途的主站请求到自己的期限后也被记成超时 —— 主站其实没坏
    (10-12 校验员用真进程池复现)。一次池重置顶多连带几条, 不够 6; 真滴流的主站每次 120s, 6 次 = 两次调用后也会切。"""
    with _FO_LOCK:
        if _FO["active"]:
            return False
        _FO["timeouts"] += 1
        if _FO["timeouts"] < FAILOVER_AFTER_TIMEOUTS:
            return False
        _switch_locked(base, backup, f"主站连续 {_FO['timeouts']} 次硬期限超时, 最后一次: {reason}")
        return True


def _note_primary_forbidden(api: str, base: str, backup: str, reason: str) -> bool:
    """主站一次网关 HTTP 403 (2026-10-28 卡 FAILOVER-2 返工 1)。**单独计** (forbidden; 不碰传输层 / 硬期限两个计数, 也不清零它们), 阈值
    FAILOVER_AFTER_FORBIDDEN=6 (与硬期限同口径), 且这一串连续 403 要**覆盖 ≥ FAILOVER_FORBIDDEN_MIN_APIS=2 个不同 api_name** 才切: 按接口的无权限
    (10-28 实录 datahubco 对 namechange) 只会卡在同一个接口上 —— 同一接口 403 多少次都不切 (切过去另一站多半也没有, 还会跟备用的 3 连败试探乒乓);
    站级封禁 (CDN/WAF 封 IP、网关按 key 封禁、镜像整站下线回 403) 会连着打到 daily / adj_factor / daily_basic —— 校验员: 以前 3 次 403 就切备用,
    一刀切成「不计」会让成批调用在主站被封时整轮全败而备用明明健康。主站任何 JSON 应答清零 (_note_primary_ok), 回切也清, 切换时与别的计数一样不清。
    满了 → 切换并返回 True (本次调用随即在备用上重来一轮); 已经切过 (本线程或别的线程) → False, 不计。api 名进日志没问题 (不是凭据)。"""
    with _FO_LOCK:
        if _FO["active"]:
            return False
        _FO["forbidden"] += 1
        apis = tuple(sorted(set(_FO["forbidden_apis"]) | {str(api)}))
        _FO["forbidden_apis"] = apis
        if _FO["forbidden"] < FAILOVER_AFTER_FORBIDDEN or len(apis) < FAILOVER_FORBIDDEN_MIN_APIS:
            return False
        shown = ", ".join(apis[:6]) + (" …" if len(apis) > 6 else "")
        _switch_locked(base, backup, f"主站连续 {_FO['forbidden']} 次 HTTP 403 覆盖 {len(apis)} 个接口 ({shown}), 最后一次: {reason}")
        return True


def _note_primary_token_error(base: str, backup: str, reason: str) -> bool:
    """主站返回 token 失效类错误 → 立即切换 (同一 key 在备用站可能仍有效: 各镜像各自校验)。"""
    with _FO_LOCK:
        if _FO["active"]:
            return False
        _switch_locked(base, backup, f"主站返回 token 失效类错误: {reason}")
        return True


def _note_primary_ok() -> None:
    """主站答了 JSON (code 0 或业务错误码都算): 传输层正常, 三个连续计数 (传输层 / 硬期限 / HTTP 403) 都清零。"""
    with _FO_LOCK:
        _FO["fails"] = 0
        _FO["timeouts"] = 0
        _FO["forbidden"] = 0
        _FO["forbidden_apis"] = ()


def _note_backup_ok() -> None:
    """备用答了 JSON (code 0, 或 token 失效 / 无权限以外的业务错误码): 备用的两个连续计数清零 (不影响「每 20 次 / 10 分钟试探主站」)。"""
    with _FO_LOCK:
        _FO["backup_fails"] = 0
        _FO["backup_timeouts"] = 0


def _thread_call(fn, args, kwargs, deadline_sec, timeout_msg: str):
    """线程版墙钟期限: 在自己的守护线程里跑 fn, 主线程最多等 deadline_sec 秒, 到点抛 TimeoutError(timeout_msg) (被弃的线程只占一个 socket,
    自己读完或 socket 超时后退出; 线程杀不死, 所以这是兜底不是主路 —— 主路是 datasource 的进程池)。试探 (_probe_call) 与进程池不可用时的
    回退 (_deadline_call) 共用。"""
    import queue as _q
    box = _q.Queue(maxsize=1)

    def _run():
        try:
            box.put((True, fn(*args, **kwargs)))
        except BaseException as e:      # noqa: BLE001  (全部带回主线程, 由调用方分类)
            box.put((False, e))
    threading.Thread(target=_run, daemon=True, name=f"tushare-{getattr(fn, '__name__', 'call')}").start()
    try:
        ok, val = box.get(timeout=deadline_sec)
    except _q.Empty:
        raise TimeoutError(timeout_msg) from None
    if ok:
        return val
    raise val


def _probe_call(fn, args, kwargs, deadline_sec):
    """试探专用的墙钟期限 (线程版, 见 _thread_call)。**刻意不走 datasource 的共享进程池**: 池是 akshare 与 Tushare 共用的 2 个工作进程,
    走池的试探 ① 撞期限会 _pool_reset() terminate 整池, 在途的健康备用请求与 akshare 请求一起被记成超时 (备用侧还会因此连着
    触发下一次试探); ② 池被两条慢请求占满时试探在队列里排队, 排队时间也算在期限内, 主站根本没收到请求就被判「试探失败」
    (2026-10-17 校验员真进程池 E2/E3/E4 复现)。试探是一次单日 trade_cal 的小 POST, 线程版够用。"""
    return _thread_call(fn, args, kwargs, deadline_sec, f"trade_cal 超过 {deadline_sec}s 墙钟 (试探线程已弃)")


def _probe_primary(token: str, base: str) -> tuple:
    """对主站打一次轻量 trade_cal (单日, 单次, PROBE_TIMEOUT_SEC socket / PROBE_DEADLINE_SEC 墙钟, 不重试, 不经共享进程池)
    -> (ok, 说明)。ok 只认 code 0 —— 业务错误码 (比如 token 仍失效) 不算恢复。**抛出的任何 Exception 都算试探失败**
    (不让调用方的 query() 因为一次试探里的意外炸掉, 位子也不会卡住); 说明里不含 token。"""
    day = time.strftime("%Y%m%d")
    payload = {"api_name": "trade_cal", "token": token,
               "params": {"exchange": "SSE", "start_date": day, "end_date": day}, "fields": "cal_date,is_open"}
    _BUCKET.acquire()
    t0 = time.time()
    try:
        j = _probe_call(_post_json, (base, payload, PROBE_TIMEOUT_SEC), {}, PROBE_DEADLINE_SEC)
    except TimeoutError:
        return False, f"trade_cal 超过 {PROBE_DEADLINE_SEC}s 墙钟"
    except (TushareError, OSError, ValueError) as e:
        return False, f"trade_cal {_redact(str(e), token)[:80]}"
    except Exception as e:      # noqa: BLE001  (MaybeEncodingError / MemoryError 一类: 也只是「这次没探通」)
        return False, f"trade_cal 试探异常 {type(e).__name__}: {_redact(str(e), token)[:60]}"
    code = j.get("code") if isinstance(j, dict) else None
    if code == 0:
        return True, f"trade_cal {day} 应答正常 {time.time() - t0:.1f}s"
    msg = j.get("msg") if isinstance(j, dict) else j
    return False, f"trade_cal code={code} {_redact(msg, token)[:60]}"


def _begin_probe_locked() -> bool:
    """调用方持 _FO_LOCK: 没人在试探就占住位子 (同一时刻只一个线程试探主站, 其余线程照走备用)。"""
    if _FO["probing"]:
        return False
    _FO["probing"] = True
    return True


def _finish_probe(ok: bool, detail: str, base: str, backup: str, why: str, level: int = logging.INFO) -> bool:
    """试探结束: 记时、放位子、计数归零; 成功即回切 (返回 True), 失败打一行留在备用。"""
    with _FO_LOCK:
        _FO["probing"] = False
        _FO["probes"] += 1
        _FO["last_probe_at"] = time.monotonic()
        if ok and _FO["active"]:
            _return_locked(base, backup, f"{why}, 主站试探成功 ({detail})")     # 里面把两边计数全部清零
            return True
        _FO["calls_on_backup"] = 0
        _FO["backup_fails"] = 0
        _FO["backup_timeouts"] = 0
        if not _FO["active"]:
            return False
        log.log(level, "tushare 主站 %s 试探失败 (%s): %s; 继续用备用 %s", _host(base), why, detail, _host(backup))
        return False


def _run_probe(token: str, base: str, backup: str, why: str, level: int = logging.INFO) -> bool:
    """占到 probing 位子的那个线程调用: 试探 → 收尾。**位子一定释放**: _probe_primary 把 Exception 都兜成「试探失败」,
    这里再用 finally 兜 BaseException (KeyboardInterrupt / SystemExit 一类) —— 否则 probing 卡成 True, 本进程再也不试探、
    永不回切 (2026-10-17 校验员场景 G: 试探里抛 RuntimeError 后拨钟 +5000s 仍直走备用)。"""
    done = False
    try:
        ok, detail = _probe_primary(token, base)
        r = _finish_probe(ok, detail, base, backup, why, level=level)
        done = True
        return r
    finally:
        if not done:
            with _FO_LOCK:
                _FO["probing"] = False
                _FO["last_probe_at"] = time.monotonic()


def _note_backup_failure(token: str, base: str, backup: str, reason: str, timeout: bool = False) -> bool:
    """备用上一次失败。两个连续计数**各算各的** (与主站同口径): timeout=True 是进程级硬期限超时 (backup_timeouts, 阈值
    FAILBACK_AFTER_TIMEOUTS=6 —— 共享进程池被别的调用的超时 terminate 时, 在途的备用请求也会被记成超时, 备用其实没坏);
    其余 (传输层 / 备用不认这把 key 的 2002·40101 / 备用无权限或没这个接口 —— JSON 业务码或网关 HTTP 403 同口径) 计 backup_fails, 阈值 FAILBACK_AFTER=3。
    满了就立即试探主站一次: 成功即回切并返回 True (本次调用随即在主站重来一轮 —— 坏备用不把主站的一次抖动放大成整轮停摆,
    备用缺的接口也能在主站补上); 失败 = 两站都不可用, 留在备用 (再满再试)。"""
    with _FO_LOCK:
        if not _FO["active"]:
            return False
        key, limit, what = (("backup_timeouts", FAILBACK_AFTER_TIMEOUTS, "硬期限超时") if timeout
                            else ("backup_fails", FAILBACK_AFTER, "失败"))
        _FO[key] += 1
        if _FO[key] < limit or not _begin_probe_locked():
            return False
        why = f"备用连续 {_FO[key]} 次{what}, 最后一次: {reason}"
    return _run_probe(token, base, backup, why, level=logging.WARNING)


def _maybe_probe_primary(token: str, base: str, backup: str) -> bool:
    """每次 query() 开头 (配了备用才调): 在备用上每 PROBE_EVERY_CALLS 次调用或 PROBE_EVERY_SEC 秒试探主站一次
    (先到先试), 成功即回切 (本次调用直接打主站)。不在备用 / 别的线程正在试探 → 什么都不做。"""
    with _FO_LOCK:
        if not _FO["active"]:
            return False
        _FO["calls_on_backup"] += 1
        since = time.monotonic() - (_FO["last_probe_at"] or 0.0)
        due_calls = _FO["calls_on_backup"] >= PROBE_EVERY_CALLS
        if not (due_calls or since >= PROBE_EVERY_SEC) or not _begin_probe_locked():
            return False
        why = f"在备用 {_FO['calls_on_backup']} 次调用" if due_calls else f"在备用 {since:.0f} 秒"
    return _run_probe(token, base, backup, why)


def failover_state() -> dict:
    """诊断用: 切换状态的副本 (见 _FO_INITIAL 的键), 不含任何凭据。"""
    with _FO_LOCK:
        return dict(_FO)


def failover_reset() -> None:
    """回到主站、全部计数清零 (用例与 credentials(refresh=True) 用)。生产里的回切走试探 (_maybe_probe_primary /
    _note_backup_failure); 每天的跑批都是新进程, 新进程从主站开始。"""
    with _FO_LOCK:
        _FO.clear()
        _FO.update(_FO_INITIAL)


# ---------------------------------------------------------------- 限频
class TokenBucket:
    """线程安全令牌桶: 平滑到 rate_per_min, 允许短时突发到桶容量。"""

    def __init__(self, rate_per_min: float):
        self.capacity = float(rate_per_min)
        self.tokens = float(rate_per_min)
        self.rate = float(rate_per_min) / 60.0
        self.ts = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, n: float = 1.0) -> float:
        """取 n 个令牌, 不够就睡到够。返回累计等待秒数。"""
        waited = 0.0
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.ts) * self.rate)
                self.ts = now
                if self.tokens >= n:
                    self.tokens -= n
                    return waited
                wait = (n - self.tokens) / self.rate
            wait = min(max(wait, 0.01), 5.0)
            time.sleep(wait)
            waited += wait


_BUCKET = TokenBucket(RATE_PER_MIN)


# ---------------------------------------------------------------- HTTP
_SESSION = None
_SESSION_LOCK = threading.Lock()


def _session():
    """进程内复用一个 requests.Session (连接复用)。工作进程各有各的。"""
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                import requests
                s = requests.Session()
                s.headers.update({"Content-Type": "application/json",
                                  "Accept": "application/json"})
                _SESSION = s
    return _SESSION


def _post_json(base: str, payload: dict, timeout: float) -> dict:
    """一次 POST 并返回解析后的 JSON。

    **必须是模块级函数**: 它要被 pickle 到工作进程里执行 (进程级硬期限)。
    这里只判传输层, 业务错误码留给主进程分类 (自定义异常跨进程 pickle 越简单越好)。
    `base` 是主进程按切换状态选好的基址 (主站或备用), 当参数传进来; 工作进程 (spawn 出来的模块副本) 的
    切换状态永远是初始值, 这里不看也不改它。
    """
    r = _session().post(base, json=payload, timeout=timeout)
    if r.status_code == 429:    # 网关限流: 不是故障 —— 主进程按超频线性退避重试, 不计入备用基址切换
        raise TushareRateLimited("HTTP 429 限流", "http", 429)
    if r.status_code == 403:    # 该站该接口无权限 (卡 FAILOVER-2, 10-28 实录: datahubco 对 namechange 回 403 而主站能服务): 不是站级传输故障 ——
        raise TushareNoPermission("HTTP 403 (该站不提供此接口或无权限)", "http", 403)   # 主进程走 JSON 无权限的同一分支: 不重试、不计站级故障
    if r.status_code != 200:
        raise TushareTransport(f"HTTP {r.status_code}", "http", r.status_code)
    try:
        return r.json()
    except Exception as e:      # noqa: BLE001  (网关返回 HTML 错误页; 错误页可能回显请求体, 先抹 token 再截断)
        body = _redact(r.text, (payload or {}).get("token", ""))[:80]
        raise TushareTransport(f"响应非 JSON: {type(e).__name__} {body!r}", "http") from e


_TC_POOL = {"disabled": False, "reason": ""}   # 本进程内 Tushare 这侧「进程池不可用, 余下走线程版」的开关 (一次即永久, 与 datasource._POOL_DISABLED 同口径)


def _pool_ping() -> int:
    """最便宜的池内调用 (--probe-backup 热身用): 模块级、可 pickle, 在工作进程里返回它自己的 pid。"""
    return os.getpid()


def pool_state() -> dict:
    """诊断用: Tushare 侧进程池回退状态的副本 (disabled / reason), 不含凭据。"""
    return dict(_TC_POOL)


def _pool_fallback_reset() -> None:
    """用例用: 回到「先试进程池」。"""
    _TC_POOL.update(disabled=False, reason="")


def _is_pool_infra_error(e: BaseException) -> bool:
    """从 datasource 执行器抛出来的异常是不是**池机制自己**的 (建池 / 提交 / 管道), 而不是 fn 在工作进程里抛的业务异常。
    工作进程里抛的异常经 multiprocessing 带回时 __cause__ 是 RemoteTraceback —— 那是 fn 的, 原样抛给调用方 (requests 的连接错也在此列);
    TimeoutError (硬期限) 与 TushareError 永远是业务的。余下: concurrent.futures 的 BrokenExecutor (含 BrokenProcessPool) / datasource 的
    _PoolUnavailable 一定是池的; PermissionError (WinError 5 DuplicateHandle) / BrokenPipeError / EOFError (管道断) 只要不是 requests/urllib3
    抛的也算池的 (requests 把 socket 错误都包成自己的 ConnectionError, 不会裸抛这三种); 其它 OSError / RuntimeError 看抛出点是否在
    multiprocessing / concurrent.futures 里。"""
    if isinstance(e, (TimeoutError, TushareError)):
        return False
    try:
        from multiprocessing.pool import RemoteTraceback
        if isinstance(e.__cause__, RemoteTraceback):
            return False
    except Exception:           # noqa: BLE001
        pass
    try:
        from concurrent.futures import BrokenExecutor
        if isinstance(e, BrokenExecutor):
            return True
    except Exception:           # noqa: BLE001
        pass
    if type(e).__name__ == "_PoolUnavailable":
        return True
    if not isinstance(e, (OSError, EOFError, RuntimeError)):
        return False
    if (type(e).__module__ or "").split(".")[0] in ("requests", "urllib3", "ssl", "http", "socket"):
        return False
    if isinstance(e, (PermissionError, BrokenPipeError, EOFError)):
        return True
    tb = e.__traceback__
    while tb is not None:
        fname = (tb.tb_frame.f_code.co_filename or "").replace("\\", "/")
        if "/multiprocessing/" in fname or "/concurrent/futures/" in fname:
            return True
        tb = tb.tb_next
    return False


def _disable_pool_side(ds, reason: str) -> None:
    """池机制不可用 → 本进程余下 Tushare 调用改走线程版硬期限, WARNING 只打一次; 同时把 datasource._POOL_DISABLED 置 True (不经它的
    _disable_pool: 那条路会去 terminate 可能已经挂住的池)。reason 由调用方抹过 token。"""
    first = not _TC_POOL["disabled"]
    _TC_POOL.update(disabled=True, reason=reason)
    if not first:
        return
    log.warning("tushare 进程池硬期限不可用 (%s) — 本进程余下 Tushare 调用改走线程版硬期限 (期限仍生效; 线程无法强杀, 失控线程不再被隔离; 只告警一次)", reason)
    try:
        if ds is not None and getattr(ds, "_POOL_DISABLED", None) is False:
            ds._POOL_DISABLED = True
    except Exception:           # noqa: BLE001
        pass


def _pool_build_budget(ds) -> float:
    """datasource 自己给建池的预算 (秒) = _POOL_DOA_WAIT_SEC + _POOL_PROBE_SEC (生产 2 + 90; 从 datasource 常量读, 不写死); 读不到按 0。"""
    try:
        return float(getattr(ds, "_POOL_DOA_WAIT_SEC", 0) or 0) + float(getattr(ds, "_POOL_PROBE_SEC", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _pool_acquire_failed(ds, e) -> None:
    """工作线程替执行器取池 (`ds._pool()`) 时抛了: 按执行器 `_call_with_deadline` 自己碰到时的口径收尾, 让它随后直接走 datasource 的线程版、**不再建一次池**
    (卡 TS-PAGINATE-3) —— 已判不可用 (就绪探针失败时 _pool() 自己 _disable_pool 过) 什么都不做; 建池 / 提交阶段的基础设施异常 (它的 _POOL_INFRA_ERRORS;
    _make_pool 抛 PermissionError [WinError 5] 那一行在 _pool() 的 try 外面, 它自己不告警) → `ds._disable_pool("建池/提交", e)` (告警一次、置 _POOL_DISABLED,
    与它自己碰到时同一句); 别的异常原样抛 (它自己碰到也是原样抛)。tushare 这侧不另告警 —— 两层各管一段。"""
    if getattr(ds, "_POOL_DISABLED", False):
        return
    disable = getattr(ds, "_disable_pool", None)
    if isinstance(e, getattr(ds, "_POOL_INFRA_ERRORS", (OSError, EOFError, RuntimeError))) and callable(disable):
        disable("建池/提交", e)
        return
    raise e


def _pool_lock_held(ds):
    """datasource 的建池锁此刻有没有线程持着 (有 = 有线程正在 _pool() 里建池 / 收拾建坏的池) -> True / False / None (看不到锁)。只试取, 不等。"""
    get = getattr(ds, "_pool_lock", None)
    if not callable(get):
        return None
    try:
        lk = get()
        if lk.acquire(blocking=False):
            lk.release()
            return False
        return True
    except Exception:           # noqa: BLE001
        return None


def _pool_busy(p) -> bool:
    """池正忙 (二次探针没轮上时看): 状态 RUN、工作进程都活着、在跑 + 排队的任务 (不算探针自己) ≥ 工作进程数 —— 探针排在别的任务后面, 不是池机制挂住。
    读的是 multiprocessing.pool.Pool 的 _state / _pool / _cache (3.8–3.14 不变); 读不到一律算不忙。"""
    try:
        workers = list(getattr(p, "_pool", None) or [])
        return (str(getattr(p, "_state", "")) == "RUN" and bool(workers) and all(w.is_alive() for w in workers)
                and len(getattr(p, "_cache", None) or ()) > len(workers))
    except Exception:           # noqa: BLE001
        return False


def _ping_pool(p, wait):
    """二次探针的那一下 (在探针自己的线程里跑): 往池 p 直接投一个 os.getpid, 最多等 wait 秒 -> 工作进程的 pid。"""
    return p.apply_async(os.getpid).get(timeout=wait)


def _pool_alive(ds, seg: str) -> tuple:
    """二次探针 (卡 TS-PAGINATE-3) -> (池机制活着?, 说明, 正在重建?)。看门线程某段到点没等到结果时调用; 探针在自己的守护线程里跑, 最多 POOL_PING_SEC + 1 秒,
    看门线程不会被它卡住。
    ① datasource 的池在 (`_POOL` 不是 None): 直接往**这个池**投一个 os.getpid (1ms 的活; 不经 _call_with_deadline —— 那条路超时会 terminate 整池, 连带在途的
       健康请求), POOL_PING_SEC 内回了 = 活着 (只是慢); 没回但池正忙 (`_pool_busy`) = 活着; 拒收 / 不作答 = 挂住。
    ② 池不在: seg="call" 而建池锁有线程持着 = 正在重建 (活着; 健康的建池有建池预算封顶); seg="pool" 时建池预算已经给足, 锁还被持着 = 建池 / 收拾建坏的池
       不回来; 没有线程持锁 = 池刚被 terminate 却不回来 (10-24 WinError 5 形态: Pool.terminate 等一个死掉的 handler 线程) —— 都算挂住。"""
    p = getattr(ds, "_POOL", None)
    if p is None:
        held = _pool_lock_held(ds)
        if held and seg == "call":
            return True, "池正在重建 (建池锁有线程持着)", True
        return False, ("池不在, 给足建池预算后建池锁仍被持着 (建池 / 收拾建坏的池不回来)" if held
                       else "池不在、也没有线程在建 (刚 terminate 却不回来, 或从没建成)"), False
    t0 = time.monotonic()
    try:
        pid = _thread_call(_ping_pool, (p, POOL_PING_SEC), {}, POOL_PING_SEC + 1, "os.getpid 探针线程没回来")
    except Exception as e:      # noqa: BLE001
        if _pool_busy(p):
            return True, f"os.getpid 探针 {POOL_PING_SEC:g}s 没轮上, 但工作进程都活着且任务占满 (正忙)", False
        return False, f"os.getpid 探针 {POOL_PING_SEC:g}s 未应答 ({type(e).__name__})", False
    return True, f"os.getpid 探针 {1000 * (time.monotonic() - t0):.0f}ms 应答 (工作进程 pid {pid})", False


def _deadline_call(fn, args, kwargs, deadline_sec):
    """把一次调用交给 `ashare.datasource` 的**进程级**墙钟期限跑, 外面再套一层看门线程 (2026-10-24 卡 TS-PAGINATE; 10-25 TS-PAGINATE-2 改宽限,
    同日 TS-PAGINATE-3 改判据)。

    懒导入是为了避开 datasource -> market -> tushare_client 的循环导入; 脱离仓库单跑 (或 datasource 导不进来) 时退化为直调, 不让基础设施问题
    变成功能缺失。优先用 `_call_with_deadline` 而不是公开的 `call_with_retry`: 后者在超时路径上会顺手把**东财**标记为不可用 —— Tushare 挂了
    不该冤枉东财 (它还是兜底源)。
    看门线程**分两段计时, 各从该段开始时算** (模块 docstring「进程池回退」): datasource 的池还没建成时先过「取池」段 —— 工作线程替执行器调一次 `ds._pool()`
    (建池, 或等别的线程建完; 池起不来按执行器自己的口径收尾, 见 _pool_acquire_failed), 上限 = 建池预算 (`_pool_build_budget`) + POOL_GRACE_SEC; 池到手后进
    「调用」段 (执行器提交并等结果, 它的硬期限 deadline_sec 从这里起算), 上限 = deadline_sec + POOL_GRACE_SEC。上一版从调用开始统一计、到点只看
    `_POOL is None`: 池在检查点前刚建好、随后的调用又接近期限就被误判挂死 (校验员 10-25 W1)。某段到点没回来先做二次探针 (`_pool_alive`): 池能作答 / 正忙 /
    正在重建 → 不回退, 再等一轮 (最多 POOL_EXTEND_MAX 轮; 探针那几秒里结果到了就直接用, 进了下一段就按新段重新计时); 探针也不回或那一轮也用完 = 池机制挂住 (10-24 非交互 Bash: spawn 子进程 PermissionError
    [WinError 5] 后 Pool.terminate 等一个死掉的 handler 线程, 永不返回), 或它抛出池机制自己的异常 (_is_pool_infra_error) → `_disable_pool_side` 一次,
    本次与余下调用改走 `_thread_call` (期限仍是 deadline_sec)。fn 自己的异常与硬期限 TimeoutError 原样抛。fn 要是模块级、可 pickle 的 (生产恒是:
    _post_json / _pool_ping / 探针的 _raw_post) —— 不可 pickle 的执行器自己走线程版, 「取池」那一步就白建了池。"""
    if not USE_PROCESS_DEADLINE:
        return fn(*args, **kwargs)
    name = getattr(fn, "__name__", "call")
    thread_msg = f"{name} 超过 {deadline_sec}s 硬期限 (线程版, 源站滴流/黑洞; 线程已弃)"
    if _TC_POOL["disabled"]:
        return _thread_call(fn, args, kwargs, deadline_sec, thread_msg)
    try:
        from . import datasource as ds
    except Exception as e:      # noqa: BLE001
        log.debug("datasource 不可用 (%s), 硬期限降级为直调", type(e).__name__)
        return fn(*args, **kwargs)
    runner = getattr(ds, "_call_with_deadline", None)
    acquire = getattr(ds, "_pool", None) if runner is not None else None
    if runner is None:          # 私有入口若被重构掉, 退回公开入口 (仍是进程级期限; 不替它取池)
        def runner(f, a, k, _dl):
            return ds.call_with_retry(f, *a, **k)
    take_pool = callable(acquire) and getattr(ds, "_POOL", None) is None and not getattr(ds, "_POOL_DISABLED", False)
    import queue as _q
    box = _q.Queue(maxsize=1)
    seg = [("pool" if take_pool else "call", time.monotonic())]    # (段, 该段开始的 monotonic): 工作线程池到手时整体换掉 (单次赋值, 看门线程不会读到半截)

    def _run():
        try:
            if take_pool:
                try:
                    acquire()                       # 「取池」: 建池 (或等别的线程建完), 按建池预算计时
                except Exception as e:              # noqa: BLE001
                    _pool_acquire_failed(ds, e)     # 池起不来: 按执行器自己的口径收尾 (它随后走线程版, 不再建一次池); 别的异常原样抛
            seg[0] = ("call", time.monotonic())
            box.put((True, runner(fn, args, kwargs, deadline_sec)))
        except BaseException as e:      # noqa: BLE001
            box.put((False, e))
    threading.Thread(target=_run, daemon=True, name=f"tushare-pool-{name}").start()
    tok = args[1].get("token", "") if len(args) > 1 and isinstance(args[1], dict) else ""
    grace, build = POOL_GRACE_SEC, _pool_build_budget(ds)
    cur, due, extended = None, 0.0, 0
    while True:
        if seg[0] != cur:                   # 新的一段 (或第一次): 上限从该段开始时算
            cur = seg[0]
            due = cur[1] + (build if cur[0] == "pool" else float(deadline_sec)) + grace
        try:
            ok, val = box.get(timeout=max(0.0, due - time.monotonic()))
            break
        except _q.Empty:
            pass
        if seg[0] != cur:                   # 恰在到点时进了下一段: 按新段重新计时
            continue
        what = (f"「取池」段超过建池预算 {build:g}s" if cur[0] == "pool" else f"「调用」段超过硬期限 {deadline_sec}s") + f" + 宽限 {grace}s"
        alive, why, rebuilding = _pool_alive(ds, cur[0])
        try:
            ok, val = box.get_nowait()      # 探针那几秒里结果到了: 用它, 不回退、不重发
            break
        except _q.Empty:
            pass
        if seg[0] != cur:                   # 探针那几秒里进了下一段 (池刚到手): 判的是已经过去的段, 不作数, 按新段重新计时
            continue
        if alive and extended < POOL_EXTEND_MAX:
            extended += 1
            more = (build if rebuilding else 0.0) + float(deadline_sec) + grace
            due = time.monotonic() + more
            log.info("tushare %s %s 仍未返回, 但二次探针说池没挂 (%s) —— 再等一轮 %gs (慢 / 忙 ≠ 池机制挂住)", name, what, why, more)
            continue
        _disable_pool_side(ds, f"{name} {what}" + (f" (又等了 {extended} 轮)" if extended else "")
                           + f" 仍未从进程池机制返回 (建池 / 就绪探针 / terminate 挂住; 二次探针: {why})")
        return _thread_call(fn, args, kwargs, deadline_sec, thread_msg)
    if ok:
        return val
    if _is_pool_infra_error(val):
        _disable_pool_side(ds, f"{type(val).__name__}: {_redact(str(val), tok)[:100]}")
        return _thread_call(fn, args, kwargs, deadline_sec, thread_msg)
    raise val


# ---------------------------------------------------------------- 主入口 (分页壳) + 单页调用
def _has_more(d):
    """回包 data 里的 has_more (官方 Tushare 新版有这个字段; 镜像不一定给) -> True / False / None (没给或看不懂)。"""
    v = d.get("has_more") if isinstance(d, dict) else None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in ("true", "false", "1", "0"):
        return v.strip().lower() in ("true", "1")
    return None


def _page_more(n_rows: int, has_more, limit: int) -> bool:
    """这一页之后还翻不翻: 服务端给了 has_more 就听它的 (True 翻 / False 停; 0 行不翻); 没给 → 行数**恰等于**本站单页上限才翻 (不足一页 = 全部;
    多于上限 = 本站上限其实更高, 回的已是全部, 不翻 —— 主站 kaixin8 一页回全市场 5,56x–5,58x 行就是这种)。"""
    if n_rows <= 0 or limit <= 0:
        return False
    if has_more is not None:
        return bool(has_more)
    return n_rows == limit


def _dedupe(api: str, df: pd.DataFrame) -> tuple:
    """各页合并后按该接口主键去重 -> (df, 去掉的行数)。PAGE_KEYS 登记的接口按登记的列 (列齐才用), 否则有 ts_code+trade_date 就按它, 否则整行。"""
    cols = set(map(str, df.columns))
    keys = PAGE_KEYS.get(api)
    if keys and set(keys) <= cols:
        subset = list(keys)
    elif {"ts_code", "trade_date"} <= cols:
        subset = ["ts_code", "trade_date"]
    else:
        subset = None
    try:
        out = df.drop_duplicates(subset=subset, keep="first").reset_index(drop=True)
    except TypeError:           # 不可哈希的单元格 (Tushare 回包都是标量, 以防万一): 不去重
        return df, 0
    return out, len(df) - len(out)


def _paginate(api: str, p: dict, fetch, limit_of) -> pd.DataFrame:
    """通用分页 (2026-10-24 卡 TS-PAGINATE; 10-25 TS-PAGINATE-2 改口径): fetch(params, pin) -> (df, has_more, base_url, role) 打一页。
    第一页按原参数 (不带 offset / limit —— 对 ≤ 上限的接口零改动), pin=None (经切换机制选站); 本站单页上限 = limit_of(第一页实际打到的基址)
    (按站配, 见 page_limit); 之后每页只多一个 offset = 已拿到的行数, **钉在第一页的那个站** (pin=(base, role)): 两站行集不同 (10-24 主站 daily 5562 行 /
    备用 5559 行, 行序也未必一致), 第一页在 A 站、续页在 B 站按 B 站的「第 5000 行之后」拼出来的表会静默丢行 (校验员 E1/E2) —— 所以切换 / 回切只在
    query() 之间生效, 续页在钉住的站上失败耗尽就整次抛, 不跨站拼页也不返回截断表。
    停止: _page_more 说停 (不足一页 / has_more=False) 或续页 0 行 → 正常返回 (合并去重; 结果 .attrs["pages"] = 页数; 多于一页才打一行 INFO
    「tushare <api>: 分页 N 页 M 行 (<host>)」, 恰满一页而续页 0 行的不打)。**截断一律抛 TushareTruncated** (带已拿到行数 / 页数 / 原因, 先打一行 WARNING
    「分页截断」): 续页失败 (重试耗尽 / 本站被判切换) / 镜像拒 offset (BadParams) / 镜像不认 offset (原样回上一页, 或续页去重后没有新行 —— 首版会翻满
    PAGE_MAX=40 页) / **续页与已拿到的行重复 = 行序不稳** (10-25 卡 TS-PAGINATE-3: PAGE_KEYS 登记了主键且列齐的接口任一行重复就判 —— 稳定的镜像不会把同一
    主键给两页, 认 offset 但每次排序不同的镜像续页大半是前页已给过的行, 并集静默少几百只, 校验员 T1; 没登记主键、按 ts_code+trade_date 或整行去重的接口
    新增不足续页一半才判, 少量重复照常去重并在 INFO 行带「去重 N 行」) / 续页行数 > 上限而服务端没给 has_more (上限配置与本站不符) / PAGE_MAX 到顶。"""
    df, more, base, role = fetch(dict(p), None)
    limit = limit_of(base)
    if not _page_more(len(df), more, limit):
        return df
    host, pin = _host(base), (base, role)
    tail = "" if not p else " " + ",".join(f"{k}={v}" for k, v in p.items())[:80]
    keys = PAGE_KEYS.get(api)
    keyed = bool(keys) and set(keys) <= set(map(str, df.columns))     # 登记了主键且列齐: 主键跨页不该重复, 一行重复就是行序不稳
    merged, _ = _dedupe(api, df)
    pages, offset = [df], len(df)

    def truncated(reason: str, cause=None):
        n_rows, n_pages = len(merged), len(pages)
        log.warning("tushare %s: 分页截断 — %s; 已拿到 %d 行 %d 页, 不返回截断表, 整次按失败处理 (%s)%s", api, reason, n_rows, n_pages, host, tail)
        err = TushareTruncated(f"{api}: 分页截断 ({host}) — {reason}; 已拿到 {n_rows} 行 {n_pages} 页, 不返回截断表", api,
                               rows=n_rows, pages=n_pages, reason=reason)
        if cause is not None:
            raise err from cause
        raise err

    while True:
        try:
            d2, more2, _b, _r = fetch(dict(p, offset=offset), pin)
        except TushareBadParams as e:
            truncated(f"offset={offset} 被拒 ({str(e)[:80]}), 镜像不支持 offset 翻页", e)
        except TushareError as e:
            truncated(f"续页 offset={offset} 失败 ({type(e).__name__}: {str(e)[:80]})", e)
        k = len(d2)
        if k == 0:
            break
        if more2 is None and k > limit:
            truncated(f"续页 offset={offset} 回 {k} 行 > 本站单页上限 {limit} (上限配置与本站不符, 或镜像不认 offset)")
        if k == len(pages[-1]) and d2.equals(pages[-1]):
            truncated(f"offset={offset} 回包与上一页相同的 {k} 行, 镜像不认 offset")
        merged2, _ = _dedupe(api, pd.concat([merged, d2], ignore_index=True))
        new = len(merged2) - len(merged)
        if new == 0:
            truncated(f"续页 offset={offset} 的 {k} 行去重后没有新行, 镜像不认 offset (行序不稳)")
        if new < k and (keyed or 2 * new < k):
            truncated(f"续页 offset={offset} 的 {k} 行里 {k - new} 行与已拿到的重复、只 {new} 行是新的 —— 行序不稳 (镜像每次排序不同或翻页期间数据在变), "
                      + ("登记了主键的接口不该跨页重复, " if keyed else "新增不足续页一半, ") + "按 offset 拼出来的表可能缺行")
        pages.append(d2)
        merged = merged2
        offset += k
        if not _page_more(k, more2, limit):
            break
        if len(pages) >= PAGE_MAX:
            truncated(f"翻到 {len(pages)} 页仍满页, 超过 PAGE_MAX={PAGE_MAX}")
    out = merged
    dropped = sum(len(x) for x in pages) - len(out)
    try:
        out.attrs["pages"] = len(pages)
    except Exception:           # noqa: BLE001
        pass
    if len(pages) > 1:
        log.info("tushare %s: 分页 %d 页 %d 行 (%s)%s%s", api, len(pages), len(out), host, f", 去重 {dropped} 行" if dropped else "", tail)
    return out


def query(api: str, fields: str = "", retries: int = MAX_RETRIES,
          deadline_sec: float = DEADLINE_SEC, paginate=None, failover_once: bool = False, **params) -> pd.DataFrame:
    """调用一个 Tushare 端点 -> DataFrame (无数据时返回带列名的空表)。

    params 里值为 None 的键会被丢掉 (方便可选参数直接传)。
    抛 TushareUnavailable (没 token) / TushareNoPermission / TushareBadParams (都不重试) /
    TushareRateLimited (含网关 HTTP 429; 线性退避后仍超频) / TushareTransport (重试耗尽) /
    TushareTruncated (分页截断: 续页拿不全, 不返回截断表, 见 `_paginate`)。
    每一页经 `_query_page` (限频 / 硬期限 / 重试 / 备用基址切换, 见其 docstring); 分页规则见模块 docstring「分页」:
    paginate=None (缺省) = 调用方没传 offset / limit 才自动翻 (研究湖 lake._pages 自己按 limit/offset 翻, 这里不插手); False = 不翻;
    True = 带 limit 也按本站上限 (`page_limit(base)`, 按第一页实际打到的站) 翻 (传了 offset 仍不翻)。行数 < 上限 (或该站 page_limit ≤ 0)
    时返回的就是那一页, 与以前完全一样。**所有页钉在第一页那个站**; 切换 / 回切只在两次 query() 之间生效。
    **failover_once=True** (2026-10-27 卡 FAILOVER-1, 模块 docstring「单次调用备用站重试」): 配了备用基址时, 本次调用在当前站跑完一轮
    (retries / 退避 / 分页规则都不变) 仍抛 TushareTransport (非 TushareBadToken) 或 TushareNoPermission (网关 HTTP 403 / JSON 无权限; 2026-10-28 卡
    FAILOVER-2: 权限按站不同, 两个方向对称) → 立即把同一调用在另一站整体重来一次 (所有页钉在那一站,
    不计数、不切换、不试探; 全局切换状态只由原规则管), 成功 INFO 一行「tushare 单次调用备用站重试」, 两站都败 WARNING 一行并按原错误类型抛出
    (消息带两站)。没配备用键 / 缺省 False 时与以前逐字节相同。给一次 update 只打一次 Tushare 的阶段 (改名表月刷 namechange) 用;
    成批调用 (按日 daily / adj_factor / daily_basic 等) **不要开** —— 它们靠全局连续计数切换, 一开就每次调用都可能多一轮。
    **墙钟**: 每页都是一次完整调用 (retries × deadline_sec + 退避), 一次 query() 最坏 = 首页最坏 + (页数 − 1) × 单页最坏, 页数 ≤ PAGE_MAX
    (全市场按日端点在备用上是 2 页 → 约翻倍; 见模块 docstring「最坏墙钟」); 按剩余预算算 deadline_sec 的调用方要按可能 2 页留预算, 或 paginate=False。
    failover_once 最坏再加一轮 (另一站的 首页最坏 + 续页)。
    """
    token, base, backup = credentials()
    if not token:
        raise TushareUnavailable(f"{api}: 未配置 tushare_token (data/secrets.json)", api)
    p = {k: v for k, v in params.items() if v is not None}
    auto = paginate is not False and "offset" not in p and (paginate is True or "limit" not in p)

    def run(forced_pin=None, used=None):
        """一次完整的逻辑调用 (单页或分页)。forced_pin=(站, None) = 单次调用备用站重试那一轮: 第一页也钉在那个站 (role None 不计数不切换),
        续页由 _paginate 按第一页实际打到的站 (= 同一站) 自己钉。"""
        def fetch(q, pin=None):
            return _query_page(api, fields, retries, deadline_sec, q, pin if pin is not None else forced_pin, used=used)
        if not auto:
            return fetch(p)[0]
        return _paginate(api, p, fetch, page_limit)

    if not (failover_once and _has_backup(base, backup)):
        return run()                            # 缺省 / 没配备用键: 与以前完全一样 (连 try 都不进)
    return _run_failover_once(api, base, backup, token, run)


def _failover_once_eligible(e: BaseException) -> bool:
    """单次调用备用站重试 (卡 FAILOVER-1) 认哪些失败: 重试耗尽后抛出的 TushareTransport —— 传输层 (HTTP 非 200 非 429 / 非 JSON / 连接错 /
    socket 超时)、进程级硬期限超时、以及 `_classify` 落到 Transport 的瞬时业务错误码 (5xx 类 / [upstream_pool_exhausted] / [minute_upstream_busy]
    一类: 它们是 JSON 应答, 全局切换机制刻意不计 —— 主站答了 JSON 就清零, 这正是 namechange 月刷三天没切过去的原因)。
    **也认** TushareNoPermission (2026-10-28 卡 FAILOVER-2): 网关 HTTP 403 (_post_json) 或 JSON 无权限 (_classify) —— 是**该站该接口**的永久性条件,
    另一站未必 (10-28 实录: 主站能服务 namechange 而备用 datahubco 回 403; 反过来主站缺 income_vip), 两个方向对称。
    **不认**: TushareBadToken (2002 / 40101 或 token+无效/过期文案: 全局机制自己立即切, 不重复处理) / TushareRateLimited (429 / 超频: 只按现有
    退避等) / TushareBadParams (参数错换站也一样) / TushareTruncated (分页截断不是传输抖动, 语义不变) / 其它异常。"""
    return isinstance(e, (TushareTransport, TushareNoPermission)) and not isinstance(e, TushareBadToken)


def _site_label(base: str, url: str) -> str:
    return "主站" if url == base else "备用"


def _strip_api_prefix(text: str, api: str) -> str:
    """异常文本一律以「<api>: 」开头 (_classify / _query_page), 拼进「主站: … / 备用: …」时去掉, 免得一行里 api 名出现三次。"""
    s = str(text or "")
    head = f"{api}: "
    return s[len(head):] if s.startswith(head) else s


def _run_failover_once(api: str, base: str, backup: str, token: str, run) -> pd.DataFrame:
    """query(failover_once=True) 且配了备用时的外壳 (2026-10-27 卡 FAILOVER-1): 先按原规则跑一轮 run(); 抛的是 `_failover_once_eligible` 认的失败
    → 立即在另一站 (本轮最后打到的站是主站就去备用, 是备用就去主站) 以 pin=(站, None) 整体重来一次 —— 不计数、不切换、不试探, 全局状态只由原规则管
    (本轮里原规则记的计数照记, 重试那一轮不再记: 不双记、不翻转)。本轮已经由全局切换在两站各来过一轮 (used 里两站都有) 就不再重来 (那只是第三轮),
    WARNING 一行后按原错误抛。成功 INFO 一行 (GM grep「单次调用备用站重试」); 两站都败 WARNING 一行, 抛**原错误类型** (type(first)), 消息带两站与
    各自原因, __cause__ = 另一站的异常。原因文本已抹过 token (_query_page / _classify), 这里再抹一次兜底。"""
    used: list = []
    t0 = time.time()
    try:
        return run(None, used)
    except TushareError as e:
        if not _failover_once_eligible(e):
            raise
        first = e
    failed_on = used[-1] if used else _active_base(base, backup)[0]
    other = backup if failed_on == base else base
    f_label, o_label = _site_label(base, failed_on), _site_label(base, other)
    reason = _redact(_strip_api_prefix(str(first), api), token)[:120]
    if other in used:
        log.warning("tushare 单次调用备用站重试: %s 本次调用已经由全局切换在 %s %s 与 %s %s 各来过一轮都失败, 不再重来 (第三轮无意义), 按原错误抛出: %s",
                    api, o_label, _host(other), f_label, _host(failed_on), reason)
        raise first
    t1 = time.time()
    try:
        df = run((other, None), [])
    except TushareError as e2:
        reason2 = _redact(_strip_api_prefix(str(e2), api), token)[:120]
        log.warning("tushare 单次调用备用站重试: %s %s %s 失败 (%s) → %s %s 也失败 (%s: %s); 两站都失败, 按原错误类型 %s 抛出 (共 %.1fs)",
                    api, f_label, _host(failed_on), reason, o_label, _host(other), type(e2).__name__, reason2,
                    type(first).__name__, time.time() - t0)
        err = type(first)(f"{api}: {f_label} {_host(failed_on)} 与 {o_label} {_host(other)} 都失败 — {f_label}: {reason}; "
                          f"{o_label}: {type(e2).__name__} {reason2}", api, first.code)
        raise err from e2
    log.info("tushare 单次调用备用站重试: %s %s %s 失败 (%s) → %s %s 成功 %d 行 (%.1fs; 本次调用共 %.1fs)",
             api, f_label, _host(failed_on), reason, o_label, _host(other), len(df), time.time() - t1, time.time() - t0)
    return df


def _permanent_failure(api: str, err: TushareError, role, use_base: str, token: str, base: str, backup: str, reason: str, how: str,
                       forbidden: bool = False, pinned: bool = False) -> bool:
    """NoPermission / BadParams 的共同出口 —— JSON 业务码路径与网关 HTTP 403 路径走**同一个分支** (2026-10-28 卡 FAILOVER-2): 永久性, 一次就抛, 不进重试。
    备用上的 NoPermission 记一次备用失败 (卡 TS-FALLBACK-3: 15000 档缺的接口): 连续满 FAILBACK_AFTER 次就试探主站, 主站回来了返回 True (调用方随即
    在主站重来本次调用); 其余 (主站 / 没配备用 / 单次换站那轮的 role None / 备用上的 BadParams / 主站没回来) 打一行 WARNING 后把 err 抛出去。
    `how` 是日志里对这次失败的称呼: JSON 业务码「永久性失败」(与以前一字不差), 网关 403「该站无权限 (HTTP 403, <host>)」(GM 按它 grep)。
    `forbidden=True` = 网关 HTTP 403 路径 (同日返工 1): 主站上进独立的连续计数 (`_note_primary_forbidden`; 传输层 / 硬期限两个计数不碰也不清零 ——
    答了 403 不算答了 JSON), 连续满 FAILOVER_AFTER_FORBIDDEN 次且覆盖 ≥ FAILOVER_FORBIDDEN_MIN_APIS 个不同接口 (站级封禁) → 切到备用, 返回 True
    (调用方随即在备用重来本次调用), 这次不打「不重试」行; JSON 业务码路径 forbidden=False: 主站计数什么都不碰 (JSON 应答的清零由调用方在进来之前做)。
    `pinned=True` = 续页 (pin 非 None): 计数照记, 但状态改了本页也不跟过去 (调用方 break → _paginate 抛 TushareTruncated), 两行「重来」日志的措辞随之改成「本页钉站不跟过去」。"""
    if role == "backup" and isinstance(err, TushareNoPermission):
        if _note_backup_failure(token, base, backup, reason):
            log.warning("tushare %s 在备用无权限 (%s), 主站已回来, %s: %s", api, _host(use_base),
                        "本页钉站不跟过去, 整次按截断处理 (续页)" if pinned else "本次在主站重来", err)
            return True
    elif role == "primary" and forbidden:
        if _note_primary_forbidden(api, base, backup, reason):
            log.warning("tushare %s 在主站 HTTP 403 (%s), 主站连续 403 已覆盖多个接口 = 站级封禁, 已切到备用, %s: %s",
                        api, _host(use_base), "本页钉站不跟过去, 整次按截断处理 (续页)" if pinned else "本次在备用重来", err)
            return True
    log.warning("tushare %s %s, 不重试: %s", api, how, err)
    raise err


def _query_page(api: str, fields: str, retries: int, deadline_sec: float, p: dict, pin=None, used=None) -> tuple:
    """一页 (一次逻辑调用, 含重试与切换) -> (DataFrame, has_more, 实际打到的基址, role)。query() 的每一页都走这里; p 已去掉 None 值。
    网关 HTTP 403 (2026-10-28 卡 FAILOVER-2) 从 _post_json 以 TushareNoPermission 抛上来, 在下面显式接住 (它不是 TushareTransport 的子类, 不接住
    会从循环里直接漏出去: 主站上碰巧像「一次就抛」, 备用上却记不到那一次备用失败也没有日志行) 并走与 JSON 无权限完全相同的 `_permanent_failure`
    (forbidden=True: 主站上进独立的 forbidden 连续计数, 同日返工 1)。
    配了备用基址时 (模块 docstring「备用基址」): 开头先看要不要试探主站 (在备用上每 20 次调用 / 10 分钟一次); 每次请求前由
    主进程按切换状态选基址 (`_active_base`); 主站连续 FAILOVER_AFTER 次传输层失败 / FAILOVER_AFTER_TIMEOUTS 次硬期限超时 / FAILOVER_AFTER_FORBIDDEN 次
    网关 HTTP 403 且覆盖 ≥ FAILOVER_FORBIDDEN_MIN_APIS 个不同接口 / token 失效类错误 → 切备用; 备用连续 FAILBACK_AFTER 次失败 (传输层 / 不认 key / 无权限)
    或 FAILBACK_AFTER_TIMEOUTS 次硬期限超时且主站试探成功 → 回主站; 切换或回切后**本次调用在新基址上重新来一轮** (完整 retries 预算, 切换瞬间不退避),
    但一次调用最多 MAX_ROUNDS_PER_CALL=2 轮。没配备用键时与 7b92bb6 版一样 (除 429、unknown_api / invalid_params 与网关 HTTP 403 三处刻意改动, 见模块
    docstring)。
    **pin=(base_url, role) = 续页** (2026-10-25 卡 TS-PAGINATE-2): 不试探主站, 每次都打钉住的那个站; 失败计数照记 (主站 3 连败照样切换状态、
    备用 3 连败照样试探主站并可能回切), 但状态改了也**不在本页跟过去重来** —— 一旦切换 / 回切发生就停止本页 (break), 整次 query() 由 _paginate
    抛 TushareTruncated, 新基址从下一次 query() 起生效 (不拼不同站的页)。**pin=(base_url, None) = 单次调用备用站重试那一轮** (2026-10-27 卡
    FAILOVER-1, `_run_failover_once`): 同样钉站不试探, 且 role None → 什么都不计、不切换 (全局状态只由原规则管)。`used` (list) 给了就把每次尝试
    实际打到的基址依次 append 进去 (外壳据此判断本轮最后打到的是哪个站、两站是否都来过)。"""
    token, base, backup = credentials()
    if not token:
        raise TushareUnavailable(f"{api}: 未配置 tushare_token (data/secrets.json)", api)
    payload = {"api_name": api, "token": token, "params": p, "fields": fields or ""}
    with_backup = _has_backup(base, backup)
    if with_backup and pin is None:
        _maybe_probe_primary(token, base, backup)       # 在备用上: 到点就试探主站, 成功即回切 (只在第一页; 续页钉站不试探)
    last: Exception | None = None
    budget = max(1, retries)
    attempt = 0
    rounds = 1
    while attempt < budget:
        attempt += 1
        if pin is not None:
            use_base, role = pin                        # 续页: 钉在第一页那个站 (或单次调用备用站重试: 钉在另一站, role None)
        else:
            use_base, role = _active_base(base, backup)     # 主进程判定; 工作进程只拿结果当参数
        if used is not None:
            used.append(use_base)
        waited = _BUCKET.acquire()
        if waited > 1.0:
            log.debug("tushare 限频等待 %.1fs (%s)", waited, api)
        t0 = time.time()
        restart = False
        try:
            j = _deadline_call(_post_json, (use_base, payload, TIMEOUT_SEC), {}, deadline_sec)
        except TimeoutError as e:                       # 进程级硬期限 (源站滴流/黑洞, 或共享进程池被别人的超时重置)
            last = TushareTransport(f"{api}: 超过 {deadline_sec}s 硬期限 ({e})", api)
            log.warning("tushare %s 硬期限超时 %d/%d (%s)", api, attempt, budget, _host(use_base))
            if role == "primary":
                restart = _note_primary_timeout(base, backup, f"{api} 超过 {deadline_sec}s 硬期限")
            elif role == "backup":
                restart = _note_backup_failure(token, base, backup, f"{api} 超过 {deadline_sec}s 硬期限", timeout=True)
        except TushareRateLimited as e:                 # 网关 HTTP 429: 限流不是故障, 线性退避重试; 不计入切换、不清零
            last = TushareRateLimited(f"{api}: {e}", api, 429)
            log.warning("tushare %s 限流 %d/%d (%s): %s", api, attempt, budget, _host(use_base), str(e)[:120])
        except TushareNoPermission as e:                # 网关 HTTP 403 (卡 FAILOVER-2): 该站该接口无权限 —— 与 JSON 无权限同一分支 (_permanent_failure):
            last = TushareNoPermission(f"{api}: {_redact(str(e), token)}", api, e.code)      # 一次就抛不重试; 主站不计传输层失败也不清零, 只进独立的 forbidden
            why = (f"{api} HTTP {e.code} (备用无权限或没这个接口)" if role == "backup"      # 计数 (返工 1: 连续 6 次且覆盖 ≥ 2 个接口 = 站级封禁才切);
                   else f"{api} HTTP {e.code}")                                                # 备用记一次备用失败
            restart = _permanent_failure(api, last, role, use_base, token, base, backup, why,
                                         f"该站无权限 (HTTP {e.code}, {_host(use_base)})", forbidden=True, pinned=pin is not None)
        except (TushareTransport, OSError, ValueError) as e:   # requests 异常 (含 socket 超时) 继承 OSError
            text = _redact(str(e), token)
            last = e if isinstance(e, TushareError) else TushareTransport(f"{api}: {text}", api)
            log.warning("tushare %s 传输失败 %d/%d (%s): %s", api, attempt, budget, _host(use_base), text[:120])
            if role == "primary":
                restart = _note_primary_transport_failure(base, backup, f"{api} {text[:80]}")
            elif role == "backup":
                restart = _note_backup_failure(token, base, backup, f"{api} {text[:80]}")
        else:
            if role == "primary":
                _note_primary_ok()          # 主站答了 JSON = 传输层正常, 连续失败清零 (业务错误码也算答了)
            code = j.get("code")
            if code == 0:
                if role == "backup":
                    _note_backup_ok()
                d = j.get("data") or {}
                cols = list(d.get("fields") or [])
                items = d.get("items") or []
                df = pd.DataFrame(items, columns=cols or None)
                log.info("tushare %s: %d 行 %.1fs%s", api, len(df), time.time() - t0,
                         "" if not p else " " + ",".join(f"{k}={v}" for k, v in p.items())[:80])
                return df, _has_more(d), use_base, role
            err = _classify(api, code, _redact(j.get("msg"), token), with_backup=with_backup)
            if role == "backup" and not isinstance(err, (TushareBadToken, TushareNoPermission)):
                _note_backup_ok()           # 备用答了 JSON (参数错 / 超频 / 其它业务错误码也算) → 备用两个连续计数清零;
                                            # 「不认这把 key」与「无权限 / 没这个接口」算备用失败 (下面)
            if isinstance(err, (TushareNoPermission, TushareBadParams)):
                # 永久性, 一次就抛 (与网关 HTTP 403 同一个出口 _permanent_failure, 卡 FAILOVER-2): 备用无权限 / 没这个接口 (15000 档比主站少) 在备用上
                # 重试没用, 但算一次备用失败 —— 连续满 3 次就试探主站, 主站回来了本次调用就在主站重来 (主站有权限); 回不来才抛给调用方 (卡 TS-FALLBACK-3)
                last = err
                restart = _permanent_failure(api, err, role, use_base, token, base, backup, f"{api} code={code} (备用无权限或没这个接口)", "永久性失败", pinned=pin is not None)
            else:
                last = err
                log.warning("tushare %s 失败 %d/%d (%s): %s", api, attempt, budget, _host(use_base), err)
                if isinstance(err, TushareBadToken):
                    if role == "primary":
                        restart = _note_primary_token_error(base, backup, f"{api} code={code}")
                    elif role == "backup":
                        restart = _note_backup_failure(token, base, backup, f"{api} code={code} (备用不认这把 key)")
        if restart:
            if pin is not None:
                log.warning("tushare %s 续页 (offset=%s) 钉在 %s: 基址状态已改 (下一次 query() 起生效), 本次不跨站续页, 整次按失败处理",
                            api, p.get("offset"), _host(use_base))
                break                   # 续页不跟着切换 / 回切过去 (不拼不同站的页); _paginate 抛 TushareTruncated
            if rounds < MAX_ROUNDS_PER_CALL:
                rounds += 1
                attempt = 0             # 刚切换/回切: 本次调用在新基址上重新来一轮, 不退避
                continue
            break                       # 已经两轮: 不再重来 (状态已改, 下一次调用会用新基址)
        if attempt < budget:
            if isinstance(last, TushareRateLimited):
                nap = BACKOFF_RATE_SEC * attempt
            else:
                nap = BACKOFF_SEC * (2 ** (attempt - 1))
            time.sleep(nap)
    raise last if last is not None else TushareTransport(f"{api}: 未知失败", api)


# ---------------------------------------------------------------- helpers
def _ymd(d) -> str:
    """'2026-09-04' / date / '20260904' -> '20260904'。禁止任何时区换算 (Tushare 日期是北京日历)。"""
    if d is None:
        return ""
    s = str(d)[:10] if not isinstance(d, str) else str(d)
    return s.replace("-", "")[:8]


def to_ts_code(sym: str) -> str:
    """本仓库代码/前端符号 -> Tushare ts_code。
    'sh000300'/'000300'/'000300.SH' -> '000300.SH'; 'sz399006' -> '399006.SZ'。
    纯 6 位无前缀时按 A 股/指数惯例: 6/9 开头 -> SH, 其余 -> SZ (000300 为沪深300, 特判)。"""
    s = str(sym or "").strip()
    if not s:
        return ""
    if "." in s:
        return s.upper()
    low = s.lower()
    if low.startswith(("sh", "sz", "bj")):
        return f"{low[2:]}.{low[:2].upper()}"
    if low.startswith("csi") or low.startswith("hs"):
        low = low[3:] if low.startswith("csi") else low[2:]
    digits = "".join(ch for ch in low if ch.isdigit())
    if len(digits) != 6:
        return s.upper()
    if digits in ("000300", "000905", "000852", "000001", "000016"):
        return f"{digits}.SH"                    # 主要宽基指数都在上交所
    return f"{digits}.SH" if digits[0] in "69" else f"{digits}.SZ"


def index_daily(ts_code: str, start, end=None, **kw) -> pd.DataFrame:
    """指数日线 -> DataFrame(trade_date, open, high, low, close, vol, amount, ...) **升序**。
    ts_code 可传 'sh000300' 之类, 内部转换。vol 单位为**手**, amount 为**千元** (Tushare 口径)。"""
    df = query("index_daily", ts_code=to_ts_code(ts_code),
               start_date=_ymd(start), end_date=_ymd(end) or None, **kw)
    if len(df) and "trade_date" in df.columns:
        df = df.sort_values("trade_date").reset_index(drop=True)
    return df


def trade_cal(start, end, exchange: str = "SSE", is_open: str = "1",
              retries: int = MAX_RETRIES, deadline_sec: float = DEADLINE_SEC) -> list:
    """开市日历 -> ['2026-09-04', ...] 升序。**任何参数下都只返开市日**: is_open=None 时服务端
    会给全部日历日, 但下面随即只留 is_open==1 —— 拿不到 is_open=0 的日子 (2026-09-08 卡 GATE-FIX
    核实并改正这句, 首版写的"is_open=None 则返回全部日历日"与代码不符; 就绪闸门的覆盖自检因此
    只能"往 target 之后问开市日", 见 leftside_core.pricestore._calendar_days)。

    `retries` / `deadline_sec` 透传给 `query`, 默认就是全局那对常量 (3 次 × 120s 硬期限,
    最坏 3×120 + 退避 2+4 ≈ 366s)。**配了备用基址时最坏翻倍再加试探** (2026-10-12 卡 TS-FALLBACK-2):
    切换/回切后在另一个基址重来一轮, 一次调用最多两轮, 外加最多两次主站试探 (各 ≤ PROBE_DEADLINE_SEC=30s):
    2×366 + 2×30 ≈ 792s ≈ 13 分钟。**跑在 systemd 定时器里、又有 TimeoutStartSec 的调用方
    必须自己收紧这两个值** —— 366s 已经单独超过 stock-tsprobe 的 5min 启动超时, 镜像滴流
    那天会让单元被 systemd 判 timeout/failed (2026-09-08 卡 DATA-B 返工据此加的这两个参数);
    retries=1 时一次调用最坏 ≈ 2×120 + 2×30 = 300s。
    """
    df = query("trade_cal", fields="cal_date,is_open", exchange=exchange,
               retries=retries, deadline_sec=deadline_sec,
               start_date=_ymd(start), end_date=_ymd(end), is_open=is_open)
    return _open_days(df)


def _open_days(df) -> list:
    """trade_cal 回包 -> 开市日 ISO 升序列表 (trade_cal 与 --probe-backup 共用)。"""
    if df is None or not len(df) or "cal_date" not in df.columns:
        return []
    if "is_open" in df.columns:
        # 任何参数下都在本地再过滤一遍: 2026-10-10 换到镜像 tl.kaixin8.top 后发现它对 start==end 的
        # 单日查询不套 is_open=1 (返回 is_open=0 的那一行), 区间查询才套 —— 客户端只数行数就把周六
        # 10-10 当成开市日, 就绪闸门等了 60 分钟拒发 10-09 产物。服务端过滤只当优化, 不当保证。
        df = df[pd.to_numeric(df["is_open"], errors="coerce") == 1]
    out = sorted({f"{str(d)[:4]}-{str(d)[4:6]}-{str(d)[6:8]}" for d in df["cal_date"]})
    return out


# ---------------------------------------------------------------- 命令行: 备用站真验收 (卡 TS-FALLBACK-2; 10-17 TS-FALLBACK-3 加日期核对)
PROBE_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount"   # 与 ashare.market.fetch_bars_by_date 同字
PROBE_DAILY_MIN_RATIO = 0.9     # 备用 daily 行数与「与主站重合的代码数」都要 ≥ 主站的这个比例才算可用 (与 pricestore 未就绪守卫 0.9 同一个数)
PROBE_NAMECHANGE_FIELDS = "ts_code,name,start_date"   # --probe-backup 的信息性 namechange 备用直调只取这几列 (卡 FAILOVER-2; 只看备用有没有这个接口)


def _direct_query(url: str, token: str, api: str, fields: str, params: dict) -> pd.DataFrame:
    """绕开切换状态, 对指定基址打 (每页单次, 不重试; 走同一个进程级硬期限) -> DataFrame。业务错误按 _classify 抛。
    与 query() 一样按该站单页上限 (`page_limit(url)`) 自动分页 (调用方传了 offset / limit 则不翻): 备用 datahubco 单页 5000 行, 不分页的话 --probe-backup 的
    「备用行数 ≥ 主站 × 0.9」永远过不了 (5000/5559 = 89.9%); 续页失败 / 镜像不认 offset → TushareTruncated (--probe-backup 把该日记成「打不通」→ rc 1)。"""
    def fetch(q, pin=None):
        payload = {"api_name": api, "token": token, "params": dict(q), "fields": fields}
        j = _deadline_call(_post_json, (url, payload, TIMEOUT_SEC), {}, DEADLINE_SEC)
        code = j.get("code") if isinstance(j, dict) else None
        if code != 0:
            raise _classify(api, code, _redact(j.get("msg") if isinstance(j, dict) else j, token))
        d = j.get("data") or {}
        cols = list(d.get("fields") or [])
        return pd.DataFrame(d.get("items") or [], columns=cols or None), _has_more(d), url, None
    p = dict(params)
    if "offset" in p or "limit" in p:
        return fetch(p)[0]
    return _paginate(api, p, fetch, page_limit)


def _daily_dates_match(df, day) -> tuple:
    """daily 回包里 trade_date 列是否**全部**等于所问的 day -> (ok, 不符的日期样例)。
    生产 (ashare.market._rows_from_daily / fetch_adj_by_date) 按 ts_code 取值、不看 trade_date, 把回包按**所问日期**写进价格库 ——
    无视或错配 trade_date 的备用站 (kaixin8 已实证会无视参数: 单日 is_open、index_member_all 的 l1_code) 切过去之后, 断更后补跑
    旧日会静默写错 K 线和复权因子; 光数行数拦不住 (10-17 校验员 J1: 问 20261016 回 20261015 的 5000 行照样 rc 0)。
    没有 trade_date 列时返回 ok (那是「缺列」, 由列检查单独判, 不重复报)。"""
    if df is None or "trade_date" not in df.columns:
        return True, []
    want = _ymd(day)
    bad = sorted({_ymd(d) for d in df["trade_date"]} - {want})
    return not bad, bad[:3]


def _codes(df) -> set:
    return set(map(str, df["ts_code"])) if df is not None and "ts_code" in df.columns else set()


def _pages_note(df) -> str:
    """回包是分页拼出来的就标「[分页 N 页]」(query / _direct_query 在结果 .attrs["pages"] 上记页数; 单页没有这个键)。"""
    try:
        n = int((getattr(df, "attrs", None) or {}).get("pages", 1))
    except Exception:           # noqa: BLE001
        n = 1
    return f" [分页 {n} 页]" if n > 1 else ""


def _warm_pool() -> str:
    """--probe-backup 开头: 先拿最便宜的 _pool_ping 把进程级硬期限这条路走通一次 -> 一句说明 (走的是进程池还是线程版)。池机制挂住 / 起不来
    (10-24 非交互 Bash 的 WinError 5 形态) 时在这里就按 _deadline_call 的两段计时 + 二次探针回退线程版, 后面 6 次调用不再各等一轮 (建池挂住最坏
    建池预算 + 宽限 = 92 + 30 ≈ 122s; 池建好后挂住 = 建池用时 + POOL_WARMUP_SEC + POOL_GRACE_SEC = 建池用时 + 20 + 30s; 二次探针把挂住误看成「活着」时再多
    一轮 —— 都有界, 不再挂死)。不打 token。"""
    if not USE_PROCESS_DEADLINE:
        return "直调 (USE_PROCESS_DEADLINE=False)"
    t0 = time.time()
    try:
        pid = _deadline_call(_pool_ping, (), {}, POOL_WARMUP_SEC)
    except Exception as e:          # noqa: BLE001
        return f"热身失败 {type(e).__name__}: {str(e)[:80]} —— 后面各次调用自行回退"
    st = pool_state()
    if st["disabled"]:
        return f"线程版硬期限 (进程池不可用: {st['reason'][:100]}) {time.time() - t0:.1f}s"
    where = "工作进程" if pid != os.getpid() else "本进程"
    return f"进程池硬期限就绪 ({where}) {time.time() - t0:.1f}s"


def probe_backup(out=print, today=None) -> int:
    """`python -X utf8 -m ashare.tushare_client --probe-backup`: 备用站**真验收**, 共 6 次调用 (备用按本站单页上限分页时 HTTP 次数多于 6,
    备用 daily 行标「[分页 N 页]」, 备用续页失败 / 镜像不认 offset → TushareTruncated → 该日「打不通」→ rc 1; 开头另有一次不联网的进程池热身
    `_warm_pool`, 池坏了在那里就回退线程版) ——
    ① 主站 trade_cal (近 3 周 + 后 1 周) 与两个开市日的 daily (窗口内最近一个开市日 D, 以及 D 往前 PROBE_SECOND_DAY_BACK 个开市日
    的 P) 走生产路径 query() (经切换机制, 每步核 failover_state()['active'] 必须是 False = 主站正常时没切);
    ② 备用站同三项**直调** (绕开切换状态, 不重试) —— 主站健康时生产请求永远到不了备用, 所以备用地址写错 / 不认这把 key 只有直调
    才看得出。判定 (每个日子各判): 备用开市日列表与主站一致; 备用 daily 行数 ≥ 主站 × PROBE_DAILY_MIN_RATIO; 与主站重合的代码数
    ≥ 主站代码数 × PROBE_DAILY_MIN_RATIO (重复行凑数的回包过不了); 带齐生产要的列; **回包 trade_date 全部等于所问日** (无视或
    错配 trade_date 的备用站, 切过去生产会按所问日期写错价格库; 第二个日子 P 专抓「一律回最新一天」的镜像 —— 它在 D 上与正确
    镜像完全一样)。退出码 0 = 备用可用 / 1 = 备用不可用 / 2 = 判不了 (没 token / 没配备用 / 主站本身失败或回包日期不对 / 验收时
    就切了)。只打 host、行数、代码数、耗时; token 永不打印 (异常文本也先抹)。
    ③ **信息性** (2026-10-28 卡 FAILOVER-2, 不改退出码): 备用直调一次 namechange (只取 PROBE_NAMECHANGE_FIELDS 几列, 按本站上限分页) —— 403 /
    无权限 → 打印「备用不提供 namechange (HTTP 403) — 月刷只能靠主站」; 成功 → 打印行数; 别的失败打一行原因。10-28 实录 datahubco 对 namechange
    回 HTTP 403 而主站能服务, 月刷 (failover_once) 会按 NoPermission 换回主站, 这里只是让 GM 加键时就看到。
    加键之前先验: PowerShell `$env:TUSHARE_BASE_URL_BACKUP='<备用完整 POST 地址>'` 再跑本命令 (credentials 认环境变量)。"""
    import datetime as _dt
    token, base, backup = credentials(refresh=True)
    if not token:
        out("判不了 (rc 2): 没配 tushare_token (data/secrets.json)")
        return 2
    if not _has_backup(base, backup):
        out("判不了 (rc 2): 没配备用基址 —— secrets.json 键 tushare_base_url_backup, 或临时 "
            "TUSHARE_BASE_URL_BACKUP=<备用完整 POST 地址> (与主站不同)")
        return 2
    today = today or _dt.date.today()
    start, end = today - _dt.timedelta(days=21), today + _dt.timedelta(days=7)
    out(f"主站 {_host(base)} / 备用 {_host(backup)}; 日历窗口 {start}..{end}; 共 6 次调用 (主站 3 走生产路径, 备用 3 直调)"
        " + 1 次信息性 namechange 备用直调 (只看备用有没有这个接口, 不影响退出码)")
    out(f"硬期限执行器: {_warm_pool()}")
    t0 = time.time()
    try:
        days_p = trade_cal(start, end)
    except Exception as e:          # noqa: BLE001
        out(f"判不了 (rc 2): 主站 {_host(base)} trade_cal 失败: {type(e).__name__}: {_redact(str(e), token)[:120]}")
        return 2
    st = failover_state()
    if st["active"]:
        out(f"判不了 (rc 2): 验收期间主站就切到了备用 ({st['reason']}) —— 先看主站, 再验备用")
        return 2
    out(f"主站 trade_cal: 开市日 {len(days_p)} 天 {time.time() - t0:.1f}s; 切换状态 active={st['active']}")
    if not days_p:
        out("判不了 (rc 2): 主站日历窗口内 0 个开市日")
        return 2
    past = [d for d in days_p if d <= today.isoformat()]
    if len(past) < 2:
        out("判不了 (rc 2): 主站日历窗口内过去开市日不足 2 天, 换个时间再验")
        return 2
    day = past[-1]
    day2 = past[-1 - PROBE_SECOND_DAY_BACK] if len(past) > PROBE_SECOND_DAY_BACK else past[0]
    prim = {}
    for d in (day, day2):
        t0 = time.time()
        try:
            df = query("daily", trade_date=_ymd(d), fields=PROBE_DAILY_FIELDS)
        except Exception as e:          # noqa: BLE001
            out(f"判不了 (rc 2): 主站 {_host(base)} daily {d} 失败: {type(e).__name__}: {_redact(str(e), token)[:120]}")
            return 2
        if failover_state()["active"]:
            out(f"判不了 (rc 2): 验收期间主站就切到了备用 ({failover_state()['reason']})")
            return 2
        n = len(df)
        ok_date, wrong = _daily_dates_match(df, d)
        out(f"主站 daily {d}: {n} 行, {len(_codes(df))} 个代码 {time.time() - t0:.1f}s" + _pages_note(df)
            + ("" if ok_date else f" —— 回包日期 {wrong} 与所问不符"))
        if n == 0:
            out(f"判不了 (rc 2): 主站 daily {d} 0 行 (镜像当日未入库?), 换个时间再验")
            return 2
        if not ok_date:
            out(f"判不了 (rc 2): 主站 daily {d} 回包日期与所问不符 —— 主站自己无视 trade_date? 先用 tools/tushare_probe.py compat 查主站")
            return 2
        prim[d] = df
    bad = []
    t0 = time.time()
    try:
        days_b = _open_days(_direct_query(backup, token, "trade_cal", "cal_date,is_open",
                                          {"exchange": "SSE", "start_date": _ymd(start), "end_date": _ymd(end),
                                           "is_open": "1"}))
        same = days_b == days_p
        out(f"备用 trade_cal: 开市日 {len(days_b)} 天 {time.time() - t0:.1f}s"
            + (" (与主站一致)" if same else f" —— 与主站不一致 (主站 {len(days_p)} 天: {sorted(set(days_p) ^ set(days_b))[:6]})"))
        if not same:
            bad.append("日历与主站不一致")
    except Exception as e:          # noqa: BLE001
        out(f"备用 trade_cal 失败: {type(e).__name__}: {_redact(str(e), token)[:120]}")
        bad.append("trade_cal 打不通或不认这把 key")
    nb = {}
    for d in (day, day2):
        t0 = time.time()
        try:
            df_b = _direct_query(backup, token, "daily", PROBE_DAILY_FIELDS, {"trade_date": _ymd(d)})
        except Exception as e:          # noqa: BLE001
            out(f"备用 daily {d} 失败: {type(e).__name__}: {_redact(str(e), token)[:120]}")
            bad.append(f"daily {d} 打不通或不认这把 key")
            continue
        df_p = prim[d]
        n_p, n_b = len(df_p), len(df_b)
        nb[d] = n_b
        codes_p = _codes(df_p)
        common = len(_codes(df_b) & codes_p)
        missing = sorted(set(PROBE_DAILY_FIELDS.split(",")) - set(map(str, df_b.columns)))
        ok_date, wrong = _daily_dates_match(df_b, d)
        notes = ([f"缺列 {missing}"] if missing else []) + ([] if ok_date else [f"回包日期 {wrong} 与所问 {_ymd(d)} 不符"])
        out(f"备用 daily {d}: {n_b} 行 ({n_b / n_p:.0%} of 主站), 与主站重合代码 {common}/{len(codes_p)} {time.time() - t0:.1f}s" + _pages_note(df_b)
            + (" —— " + "; ".join(notes) if notes else ""))
        if n_b < PROBE_DAILY_MIN_RATIO * n_p:
            bad.append(f"daily {d} 行数 {n_b} < 主站 {n_p} × {PROBE_DAILY_MIN_RATIO:g}")
        if common < PROBE_DAILY_MIN_RATIO * len(codes_p):
            bad.append(f"daily {d} 与主站重合代码 {common} < {len(codes_p)} × {PROBE_DAILY_MIN_RATIO:g}")
        if missing:
            bad.append(f"daily {d} 缺列 {missing}")
        if not ok_date:
            bad.append(f"daily {d} 回包日期与所问不符 (备用无视或错配 trade_date, 生产会按所问日期写错价格库)")
    # 信息性 (卡 FAILOVER-2): 备用有没有 namechange —— 不进 bad, 不改退出码
    t0 = time.time()
    try:
        df_n = _direct_query(backup, token, "namechange", PROBE_NAMECHANGE_FIELDS, {})
        out(f"备用 namechange (信息性, 不影响退出码): {len(df_n)} 行 {time.time() - t0:.1f}s" + _pages_note(df_n))
    except TushareNoPermission as e:
        why = "HTTP 403" if e.code == 403 else f"code={e.code} {_redact(str(e), token)[:60]}"
        out(f"备用不提供 namechange ({why}) — 月刷只能靠主站 (信息性, 不影响退出码; 月刷的单次调用备用站重试会按 NoPermission 换站)")
    except Exception as e:          # noqa: BLE001
        out(f"备用 namechange 失败: {type(e).__name__}: {_redact(str(e), token)[:120]} (信息性, 不影响退出码)")
    if bad:
        out(f"结论 (rc 1): 备用 {_host(backup)} 不可用 —— " + "; ".join(bad) + "; 不要加键 / 加了就去掉")
        return 1
    out(f"结论 (rc 0): 备用 {_host(backup)} 可用 —— 日历一致 {len(days_p)} 天, daily {day} {nb[day]}/{len(prim[day])} 行, "
        f"daily {day2} {nb[day2]}/{len(prim[day2])} 行 (两天回包日期都对, 代码重合 ≥ {PROBE_DAILY_MIN_RATIO:.0%}); "
        f"主站 {_host(base)} 正常, 切换状态 active=False")
    return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -X utf8 -m ashare.tushare_client",
                                 description="Tushare 客户端自检 (只打 host / 行数 / 耗时; token 永不打印)")
    ap.add_argument("--probe-backup", action="store_true",
                    help="备用基址真验收: 6 次调用 (主站 trade_cal + 两个开市日的 daily 走生产路径, 备用同三项直调; 核日历 / 行数 / "
                         "代码重合 / 回包日期) + 1 次信息性 namechange 备用直调 (不影响退出码); 退出码 0 可用 / 1 不可用 / 2 判不了")
    ap.add_argument("--state", action="store_true", help="打印本进程的切换状态与进程池回退状态 (不联网)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if a.probe_backup:
        return probe_backup()
    if a.state:
        print(failover_state())
        print(pool_state())
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    import importlib
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:           # noqa: BLE001
            pass
    # 用包里那份模块跑 (不是 __main__ 这份副本): datasource/market 与进程池看到的是同一个模块实例, 切换状态只有一份
    sys.exit(importlib.import_module("ashare.tushare_client").main())
