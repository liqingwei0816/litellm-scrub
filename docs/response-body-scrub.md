# 响应体字段清洗：契约、取证与验收

配套实现 `custom_logger.py`，规则文件 `scrub_rules.yaml`。本文只讲三件事：
**承诺是什么**、**承诺是被什么证据证明的**、**明知有代价仍然接受了什么**。

---

## 1. 语义决策

原语义是「命中字段直接 `del` 掉键」。现语义为**替换语义 + 结构完全守恒（方案 D）**：
递归进入命中路径下的容器，把其中**所有字符串叶子**原地替换成 `SCRUB_PLACEHOLDER`，
键名、值的 JSON 类型、每层数组的元素个数、嵌套结构全部与原始一致。

### 契约表

| # | 条款 | 实现 | 测试锚点 |
|---|---|---|---|
| C1 | 命中路径下的字符串叶子一律替换为占位符 | `_scrub_subtree` :202（写入 :244） | `test_leaf_string_target_preserves_keys_and_type`、`test_container_target_replaces_all_nested_strings`，变异 M01 |
| C2 | **绝不删键**（旧删除语义即回归） | `_scrub_subtree` :202 | `test_differential_old_vs_new_only_differs_where_contract_says`、`test_property_structure_preserved_for_container_target`，变异 M02 |
| C3 | 非字符串叶子（int/float/bool/null）原样保留 | `_scrub_subtree` :249-256 | `test_non_string_leaves_are_not_replaced`，变异 M03 |
| C4 | 每层数组元素个数不变；空容器保留 | `_scrub_value` :260 / `_scrub_path` :280 | `test_terminal_wildcard_keeps_array_length`、`test_empty_containers_kept`、`test_scrub_path_terminal_index` |
| C5 | 遍历限深 `MAX_SCRUB_DEPTH`，超出截停并报 capped | :120 / :250 | `test_pathologically_deep_payload_does_not_hit_recursion_limit`、`test_depth_cap_reports_and_stops`、`test_deeply_nested_does_not_recursion_error`、`test_self_referential_structure_terminates`，变异 M04 |
| C6 | 幂等：占位符不计入 replaced，第二趟 `(0,0,0)` | :243 | `test_idempotent_container_target_counts_do_not_inflate`、`test_idempotent_no_nesting`、`test_second_pass_is_free`，变异 M05 |
| C7 | 占位符不含长度/类型/hash；`saved` 记账真实 | :113 / :248 | `test_placeholder_carries_no_metadata`、`test_no_long_substring_of_original_survives`、`test_scrub_slo_response_applies_every_path`，变异 M06 |
| C8 | `[*]` 必须逐项处理（终点与中间通配都是） | `_scrub_path` :319（终点 :325 / 中间 :331） | `test_every_element_of_wide_array_is_replaced_not_just_first`、`test_scrub_path_wildcard_on_non_list_is_noop`，变异 M07 / M08 |
| C9 | 命中前缀归一化（小写、去尾 `/`）+ 边界校验 `/ : ? #` | `_normalize_base` :155 / `_match_rule` :430（边界 :450） | `test_match_rule_boundaries`、`test_tokenize_edge_cases`，变异 M10 / M11 |
| C10 | **客户端响应对象原样透传，绝不修改 `result`** | `async_logging_hook` :611（返回 :639） | `test_async_hook_scrubs_and_leaves_result_object_untouched`、`test_async_hook_noop_when_rule_misses`，变异 M12 + 逐字节 A/B |
| C11 | `api_base` 取值优先级：`litellm_params.metadata.api_base` 最优先 | `_extract_api_base` :455 | `test_extract_api_base_priority_order`、`test_extract_api_base_falls_through_empty_strings`，变异 M13 |
| C12 | 命中规则却零替换 → WARNING（规则写错/过期不静默） | `_scrub_and_report` :574 | `test_async_hook_warns_when_matched_but_nothing_replaced`、`test_logging_hook_miss_is_silent_and_mismatch_warns`、`test_async_hook_warns_when_depth_capped`，变异 M14 |
| C13 | 两个钩子共用一份"清洗 + 留痕"实现，语义与日志完全一致 | `_scrub_and_report` :541（`async_logging_hook` :611 / `logging_hook` :641） | `test_both_hook_paths_emit_identical_logs`、`test_logging_hook_scrubs_like_async_path`、`test_logging_hook_from_running_loop`、`test_logging_hook_never_touches_asyncio`，变异 M15 / M17 |
| C14 | 原地改写，不复制整棵响应树 | `_scrub_slo_response` :499（遍历 :531） | `test_scrub_is_in_place_no_whole_tree_copy`、`test_wide_array_scales_linearly`、`test_hook_overhead_on_realistic_payload_is_small`，变异 M16 |
| C15 | 任何异常 → WARNING 后原样入库（fail-open，但留痕） | :622-627 / :651-656 | `test_async_hook_swallows_exceptions_and_keeps_pipeline`、`test_async_hook_never_raises_on_malformed_kwargs`、`test_logging_hook_swallows_scrub_failure` |
| C16 | 体积增量 ≤ `Σ max(0, len(占位符) − len(原串))`，不承诺"任何载荷都变小" | :248 / :273 记账 | `test_worst_case_size_bound_all_short_strings`、`test_real_payload_shrinks_dramatically` |
| C17 | 整数型环境变量写了非法值 → WARNING 并退回默认，绝不让 import 期失败 | `_int_env` :80 | `test_int_env_tolerates_bad_values`，变异 M18 |
| C18 | `kwargs` 任意一层不是 dict 都不得抛异常（抛异常 = 整次清洗被跳过） | `_as_dict` :144 | `test_extract_api_base_tolerates_non_dict_layers`，变异 M20 |
| C19 | 形状不符（slo / response 不是 dict）必须指名道姓地告警；缺失（None）刻意静默 | `_scrub_slo_response` :515 / :523 | `test_slo_shape_mismatch_is_reported_precisely`，变异 M21 |
| C20 | 同一前缀写两条规则 → 启动期 ERROR（第二条的 `scrub_paths` 永不生效） | `_load_rules` :401 | `test_loader_reports_duplicate_prefix`、`test_first_of_duplicate_prefixes_wins`，变异 M19 |

规则加载与兼容性由另一组测试守：`test_loader_accepts_scrub_paths_and_deprecated_remove_paths`、
`test_loader_fails_loud_on_invalid_path`、`test_loader_rejects_too_many_tokens`、
`test_loader_missing_file_is_not_fatal`、`test_loader_empty_and_broken_yaml_are_not_fatal`、
`test_loader_skips_rules_missing_fields`、`test_repo_rules_file_loads_with_scrub_paths`、
`test_scrub_slo_response_applies_every_path`（变异 M22：一条规则下的路径必须全部执行）。
入库形状合法性由 `test_scrubbed_output_validates_against_official_schemas`（jsonschema 对
官方 OpenAI schema）守。

### 为什么结构守恒是硬约束而不是审美

- `LiteLLM_SpendLogs.response` 在镜像内 `/app/schema.prisma:578` 是 `Json?`，落到
  Postgres 即 **jsonb**，合法性由数据库校验。
- 用户原始诉求就是「保留 json 完整结构」；方案 D 是候选口径中最严格的一档。
- 上游有同类先例：`litellm/litellm_core_utils/redact_messages.py` 对叶子字符串一律赋
  `"redacted-by-litellm"`（:111/:115/:104），占位符同样不带元信息。**但**上游对容器字段
  赋 `None`/`[]`（:87/:206，元素数不守恒），本项目刻意比上游更严格，不是照抄。
- 遍历机制与上游 `logging_utils.py:78-124`（`_truncate_base64_in_value`）一致：显式栈、
  限深、只动字符串。上游限深取 20（:48），本项目取 64 留余量。

---

## 2. 一手取证：被推翻的原假设

仓库原有说明声称「LiteLLM 的截断会破坏 JSON 结构，所以需要我们来修」。读 v1.83.14 源码
后**该说法不成立**：

- 截断实现在 `litellm/proxy/spend_tracking/spend_tracking_utils.py:884` 调用的
  `_sanitize_request_body_for_spend_logs_payload`（:665-696），阈值
  `MAX_STRING_LENGTH_PROMPT_IN_DB` 默认 2048（`litellm/constants.py:387`）。
- 它是**逐字符串**保留头 35% + 尾 65%，只改字符串内容、不改 JSON 结构。

真正成立的问题有两个，也正是本功能的价值所在：
1. **行体积无上界**：一行里有多少个超长字符串、每个都留 2048 字符，没有任何约束。
2. **敏感载荷残留**：一段 base64 里有 1300+ 字符原样留在库里。

结论：本功能不是"救 JSON 合法性"，而是"给行体积一个上界 + 把残留内容清零"。
文档与注释里凡按前者立论的地方都已改正（`docker-compose.yml` 的
`MAX_STRING_LENGTH_PROMPT_IN_DB` 注释块即为此说明）。

---

## 3. 两个钩子，一份实现

经核实 v1.83.14 源码，LiteLLM 有**两条**会调用本模块的路径，两处都拿返回值直接解包赋值
（`model_call_details, result = ...`），所以两个钩子都必须返回二元组，漏掉任何一个都等于
在那条路径上完全不清洗：

1. `async_logging_hook` :611 —— 异步路径（proxy 常态），`litellm_logging.py:2670` / `:2679` 处 `await`。
2. `logging_hook` :641 —— 同步路径，`litellm_logging.py:2104` / `:2110` 处调用。

两者都只做一件事：调用 `_scrub_and_report` :541，然后原样返回 `(kwargs, result)`。

**为什么同步钩子不碰 asyncio**：清洗全程没有任何 `await`。旧实现把协程丢进事件循环跑一遍，
白付一次调度开销，还要处理"循环正在跑就不能 `run_until_complete`""线程里取不到当前循环"
这些边界；而 `asyncio.get_event_loop()` 自 3.12 起已弃用、3.14 起在没有当前循环时直接抛
`RuntimeError`。删掉之后两条路径的行为天然一致（`test_logging_hook_never_touches_asyncio`
把三个取循环的 API 全部炸掉，清洗照做）。

**为什么"清洗 + 留痕"只能有一份**：旧实现 async 与 sync 各写了一份日志逻辑，结果 sync
那份漏了"超出限深"的 WARNING —— 同一个超深载荷走主路径会告警、走兜底路径就静默。而
"清洗没有覆盖全"恰恰是本项目最不能静默的事件。合并后由 `test_both_hook_paths_emit_identical_logs`
断言两条路径的清洗结果与日志逐条一致，并由变异 M15（只让 `origin != "sync"` 时报 capped）守着。

清洗只作用于 `standard_logging_object["response"]`，因为经核实 SpendLogs 的 `response`
正是取自这里；`result` 是已返回给客户端的对象且与之不共享引用，改动它不仅无收益，还会让
下游回调（如 `_ProxyDBLogger` 的 `hasattr(result, "choices")` 判断）静默失效。

---

## 4. 可观测性的真实情况

**默认部署下本模块的 INFO 日志不可见。** proxy 未把 root logger 调到 INFO 时只有
WARNING/ERROR 输出。因此安全类信号必须（也确实）用 WARNING：

- 命中规则却零清洗：`scrub rule matched but replaced nothing (rule prefix=…, paths=…, call_id=…, hook=async|sync)`
- 超出限深：`scrub stopped at N container(s) beyond MAX_SCRUB_DEPTH=64; their string leaves were NOT replaced`
- 形状不符（上游把 slo 换成非 dict，或 `response` 不是 dict）：`standard_logging_object is X, not a dict; response will be stored as-is`、
  `standard_logging_object['response'] is X, not a dict; stored as-is`
  —— 与"零清洗"分开报，否则排查会被引向"检查路径"这个错误方向
- 配置项非法（`MAX_SCRUB_DEPTH=64x`、负数）：`… is not an integer; falling back to N`、`… must not be negative; falling back to N`
- 规则文件缺失：`scrub rules file not found at …; scrubbing disabled`
- 钩子自身炸了：`async_logging_hook failed; writing as-is`、`logging_hook failed; writing as-is`（带堆栈）
- 路径语法非法 / 记号过多 / **同一前缀写了两条规则**：启动期 `logger.error`
- 旧键名：`scrub rule uses deprecated key 'remove_paths'; rename to 'scrub_paths'`

逐请求的清洗条数统计是 INFO（含 `hook=` 标签用于区分是哪条路径触发的），需要显式放开
日志级别才看得到。实测留痕：把规则前缀改成不可能命中制造"零清洗"场景时，容器日志里
确实打出上述 WARNING 原文。

**容器日志本身也会泄露原始响应**：`--detailed_debug` 等同 `litellm._turn_on_debug()`
（`proxy_cli.py:952`），会让 `litellm_logging.py:1231` / `:1240` 以 DEBUG 打出
`RAW RESPONSE:\n{}`，把清洗前的整段 base64 原样写进 `docker logs`。清洗只管入库，管不到
这里，所以 `docker-compose.yml` 已刻意去掉该开关（排查时临时加，用完删）。同文件里端口也
改为只绑回环 `127.0.0.1:4000:4000` —— 这个代理带着 master key 和宿主机 Postgres 凭据，
不该默认对整个局域网开放。

---

## 5. 已接受风险（明知有代价，不作为缺陷反复提出）

1. **结构即侧信道**：入库数据保留原始键名与每层数组长度。选方案 D 等于接受这一点——
   攻击者能推断响应形状，但拿不到内容与长度量级（占位符不带元信息）。
2. **体积不再是 O(1)**：净开销随字符串叶子数线性变化，且极短字符串被替换后可能变大
   （C16）。因此性能测试是发布门禁的一部分，不是可选项：1 万元素数组、5000 层深嵌套、
   1.43 MiB 真实形状载荷的 hook 耗时都已入测试并有阈值断言，实测数字见 §6。
   关键性质是**开销取决于"访问到的节点数"，不是载荷字节数**：一个 1.43 MiB 的 base64
   只要落在路径终点的单个字符串叶子上，整条 hook 只花 2.2 µs。
3. **规则不热更新**：`_RULES` 在模块导入时构建（:427），改文件必须重启容器。陈旧规则
   会 fail-open（数据原样入库）但有 WARNING 兜底。
4. **清洗只覆盖 SpendLogs 这一条出口**：本模块改的是 `standard_logging_object["response"]`，
   因此只对写库生效。容器日志（见 §4 的 `--detailed_debug`）、以及将来若接入的
   Langfuse / S3 / 自定义 callback 等其它 sink 都在范围之外，需要各自处置。
5. **失败模式是 fail-open**：清洗链路上任何一处抛异常，结果都是"原样入库 + 一条 WARNING"，
   而不是"拒绝写日志"。这是刻意的取舍（日志管道崩掉影响的是所有请求），代价是
   必须有人真的在看 WARNING —— 所以 §4 里能被看见的信号一律用 WARNING/ERROR。

---

## 6. 验收证据（2026-10-17 / 10-18 实测，全部可复跑）

| 验收项 | 证据 |
|---|---|
| 结构完整性 | 库内同载荷「清洗 / 未清洗」两行：键路径 20 vs 20、集合全等、值类型映射全等、差异路径为空 |
| 数据库侧 | 命中行 454 B 且 `base64残留=false`；未命中对照行 570 B 且残留=true；`jsonb_typeof=object`，键集 `id,model,usage,object,choices,created,system_fingerprint` |
| 体积 | 非流式 570→454、流式 748→632，均为 −116 = `len(content) 126 − len("<scrubbed>") 10`，与 C16 公式吻合 |
| 不泄露 | 大载荷轮（config 临时换成 11902 字符 base64）：清洗行 0/990 个片段泄露，对照行 167/990 |
| 客户端零影响（非流式） | 清洗开 / 关 / 再开三态响应体（剔除 `id`/`created`）sha256 均为 `2f7685d5efbe200f…197db1b` |
| 客户端零影响（流式） | 三态 SSE 各 8039 B / 43 chunk，归一化 sha256 均为 `24a514a1fc68edb6…db35a7dc`，重组 content 三态都是完整 base64 |
| 门禁 | 82 passed；`custom_logger.py` 219 stmts / 0 miss / 102 branch / 0 partial = 100%；变异 22/22 killed（控制组先须全绿）；ruff 0.17.1（E,W,F,SIM,RUF，排除中文全角标点误报 RUF001/002/003）全绿 |
| 耗时（2026-10-18 实测，容器内，取 5 轮最小值） | 1.43 MiB 真实形状载荷整条 hook **2.2 µs**（目标路径只有一个字符串叶子，开销随"访问到的节点数"而非字节数变化）；1 万元素宽数组 + 容器终点 **4.90 ms**；5000 层深链 **11.6 µs**（限深 64 截停）。门禁阈值分别是 < 50 ms / < 2 s / < 1 s |
| 热路径分配（A/B） | 把 `list(enumerate(node))` 改成惰性视图后，同一台机器同一容器内 1 万元素宽数组：4.39 ms → 3.99 ms（**−9.1%**）；1000 元素 −3.8%；单大字符串与深链两种形状无可测差异（它们不走数组遍历） |

未纳入门禁：**mypy**（钉死镜像与 `/deps` 均无该工具，未为此拉第二套工具链）。
`tests/` 下 `test_custom_logger.py` 是 pytest 套件，`mut_harness.py` 是变异门禁（不是 pytest
用例，直接当脚本跑）；鉴权那个脚本式测试已随功能移出项目。

上表的**结构完整性 / 体积 / 不泄露 / 客户端零影响（流式）**四行是 2026-10-17 在真实
proxy + Postgres 上取的证，当时代码是重构前的版本；重构只动了"日志留痕、健壮性、
配置解析"，清洗语义与写入路径未变（由 C1-C16 的测试与 22 条变异守着）。

2026-10-18 在重构后的代码上重跑了一遍非流式端到端（`docker compose up -d` 重建容器）：

- 客户端拿到的 hit-model 响应仍是**完整 base64**（C10 零影响）；miss-model 原样。
- 库内 hit 行 454 B、`content = <scrubbed>`、`base64残留 = false`、`jsonb_typeof = object`，
  顶层键集 `id,model,usage,object,choices,created,system_fingerprint` 与 10-17 完全一致；
  `choices` 仍是长度 1 的数组、`content` 仍是 string、`usage.total_tokens` 仍是数字。
- miss 行 496 B 原样入库。
- 容器日志里 `RAW RESPONSE` 出现 **0 次**（去掉 `--detailed_debug` 的效果，见 §4）。

**门禁 / 耗时 / 热路径分配**三行同为 2026-10-18 在重构后的代码上测得。

---

## 7. 复跑指南

### 7.1 单测 / 覆盖 / lint / 变异

门禁必须在钉死的镜像内跑（`litellm` 是真实导入，不用 stub）。依赖是跨平台下载的
linux cp313 wheel 解包目录，bind-mount 到 `/deps`：

```sh
export MSYS2_ARG_CONV_EXCL='*'          # Windows/MSYS 下必须，否则 /work 会被改写成盘符路径
IMG=ghcr.io/berriai/litellm:v1.83.14-stable
PY=/app/.venv/bin/python3

# 单测 + 分支覆盖
docker run --rm -v "$PWD:/work" -v <deps>:/deps -w /work -e PYTHONPATH=/deps \
  -e SCRUB_RULES_PATH=/work/scrub_rules.yaml -e HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hyp \
  -e COVERAGE_FILE=/tmp/cov.data -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
  --entrypoint $PY $IMG \
  -m pytest tests/test_custom_logger.py -q --no-header -p no:cacheprovider \
  --cov=custom_logger --cov-report=term-missing --cov-branch

# lint（ruff 用 manylinux wheel 解包出的二进制，进容器需 chmod +x）
docker run --rm -v "$PWD:/work" -v <ruff-bin-dir>:/ruffbin -w /work \
  --entrypoint /ruffbin/ruff $IMG check --isolated --select E,W,F,SIM,RUF \
  --ignore RUF001,RUF002,RUF003 --line-length 100 custom_logger.py tests/

# 变异测试（22 条；只跑其中几条就在末尾追加编号，如 tests/mut_harness.py M01 M16）
docker run --rm -v "$PWD:/work" -v <deps>:/deps -w /work -e PYTHONPATH=/deps \
  -e SCRUB_RULES_PATH=/work/scrub_rules.yaml -e HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hyp \
  -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
  --entrypoint $PY $IMG tests/mut_harness.py
```

`RUF001/002/003` 必须排除：它们把中文全角标点判成"歧义 Unicode 字符"，在本项目的注释
风格下会产出 700+ 条纯噪声（实测 761 条里 757 条是这三条规则）。除此之外 E/W/F/SIM/RUF
全绿，`--line-length 100`。

变异脚本 `tests/mut_harness.py` 对 `custom_logger.py` 施加 22 条语义变异，每条锚定上表
一行契约，**锚点要求在源码中恰好出现一次**：命中次数不是 1 就判 INVALID（而不是假装杀死），
所以重构挪动代码后锚点失效会立刻暴露。控制组（未变异的原实现）不绿时直接中止——
否则"全红"会被误读成"变异全被杀死"。

### 7.2 端到端 A/B（客户端逐字节）

单代理串行跑，**不要并发起两个代理容器**（实测会把 Docker Desktop 引擎打到所有 API 返回 500）：

1. 校验 `scrub_rules.yaml` 的 sha256 是仓库原版（`416012233d5f338e…f30a71c`）。
2. 发请求 → 记录响应体与 `id`（清洗开 A）。
3. 把 `api_base_prefix` 改成 `https://provider-a.invalid`（等效关闭清洗）→ 重启 → 发同样请求（关）。
4. 改回并再次校验 sha256 → 重启 → 发第三次请求（清洗开 B）。
5. 剔除 `id`/`created` 后对三次的 JSON 归一化求 sha256，断言三者相等。
6. 数据库侧回读三条行。

流式同理，把请求体换成 `{"model":"hit-model","stream":true,…}`，收整条 SSE，
按 chunk 解析后剔除每个 chunk 的 `id`/`created` 再比对。

### 7.3 数据库侧断言

```sql
-- 结构与体积：逐行看清洗/未清洗
select request_id, jsonb_typeof(response), octet_length(response::text),
       (position('base64' in response::text) > 0) as base64_residue,
       response #>> '{choices,0,message,content}' as content
  from "LiteLLM_SpendLogs" order by "startTime" desc limit 5;
```

坑（都踩过）：

- **`startTime` 是 UTC**，比宿主机时间少 8 小时；时间窗过滤按 UTC 写。
- 列名是 `"startTime"` / `status`，**没有** `callTime` / `error`。
- **spend-log 是异步写入**：请求返回后至少等 25 秒再读，且**不要**在读回之前重启——
  否则那一行会整条消失，容易被误读成"关闭清洗导致写入失败"。
- 该 Postgres 无 pgcrypto，`digest()` 不可用；sha256 在宿主机算。
- `psql -f -` 在本机 MSYS 下会报 `extra-invalid-parameter-value`，改用逐条 `-c`。
- 键路径集合与类型映射的递归比对用 Python 做更省事，`jsonb_each` 的递归 CTE 不接受 lateral。
