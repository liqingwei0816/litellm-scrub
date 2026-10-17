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
| C1 | 命中路径下的字符串叶子一律替换为占位符 | `_scrub_subtree` :165 | `test_leaf_string_target_preserves_keys_and_type`、`test_container_target_replaces_all_nested_strings`，变异 M01 |
| C2 | **绝不删键**（旧删除语义即回归） | `_scrub_subtree` :165 | `test_differential_old_vs_new_only_differs_where_contract_says`、`test_property_structure_preserved_for_container_target`，变异 M02 |
| C3 | 非字符串叶子（int/float/bool/null）原样保留 | `_scrub_subtree` :216 | `test_non_string_leaves_are_not_replaced`，变异 M03 |
| C4 | 每层数组元素个数不变；空容器保留 | `_scrub_subtree` / `_scrub_value` :220 | `test_terminal_wildcard_keeps_array_length`、`test_empty_containers_kept` |
| C5 | 遍历限深 `MAX_SCRUB_DEPTH`，超出截停并报 capped | :94 / :210 | `test_pathologically_deep_payload_does_not_hit_recursion_limit`、`test_depth_cap_reports_and_stops`、`test_deeply_nested_does_not_recursion_error`、`test_self_referential_structure_terminates`，变异 M04 |
| C6 | 幂等：占位符不计入 replaced，第二趟 `(0,0,0)` | :203 | `test_idempotent_container_target_counts_do_not_inflate`、`test_idempotent_no_nesting`、`test_second_pass_is_free`，变异 M05 |
| C7 | 占位符不含长度/类型/hash；`saved` 记账真实 | :87 / :208 | `test_placeholder_carries_no_metadata`、`test_no_long_substring_of_original_survives`，变异 M06 |
| C8 | `[*]` 必须逐项处理（终点与中间通配都是） | `_scrub_path` :240 | `test_every_element_of_wide_array_is_replaced_not_just_first`、`test_scrub_path_wildcard_on_non_list_is_noop`，变异 M07 / M08 |
| C9 | 命中前缀归一化（小写、去尾 `/`）+ 边界校验 `/ : ? #` | `_normalize_base` :118 / `_match_rule` :376 | `test_match_rule_boundaries`、`test_tokenize_edge_cases`，变异 M10 / M11 |
| C10 | **客户端响应对象原样透传，绝不修改 `result`** | `async_logging_hook` :472 | `test_async_hook_scrubs_and_leaves_result_object_untouched`、`test_async_hook_noop_when_rule_misses`，变异 M12 + 逐字节 A/B |
| C11 | `api_base` 取值优先级：`litellm_params.metadata.api_base` 最优先 | `_extract_api_base` :401 | `test_extract_api_base_priority_order`，变异 M13 |
| C12 | 命中规则却零替换 → WARNING（规则写错/过期不静默） | :520 / :601 | `test_async_hook_warns_when_matched_but_nothing_replaced`、`test_sync_scrub_misses_and_warns`、`test_async_hook_warns_when_depth_capped`，变异 M14 |
| C13 | 兜底路径 `_sync_scrub` 语义与主路径一致 | :584（共用 `_scrub_slo_response` :441） | `test_sync_scrub_path_matches_async_semantics`、`test_logging_hook_from_running_loop`、`test_logging_hook_without_running_loop`、`test_logging_hook_with_idle_event_loop`，变异 M15 |
| C14 | 原地改写，不复制整棵响应树 | `_scrub_slo_response` :441 | `test_scrub_is_in_place_no_whole_tree_copy`、`test_wide_array_scales_linearly`、`test_hook_overhead_on_realistic_payload_is_small`，变异 M16 |
| C15 | 任何异常 → WARNING 后原样入库（fail-open，但留痕） | :544 / :618 | `test_async_hook_swallows_exceptions_and_keeps_pipeline`、`test_async_hook_never_raises_on_malformed_kwargs`、`test_logging_hook_falls_back_when_loop_lookup_fails`、`test_logging_hook_final_except_returns_unchanged`、`test_sync_scrub_exception_is_caught_and_logged` |
| C16 | 体积增量 ≤ `Σ max(0, len(占位符) − len(原串))`，不承诺"任何载荷都变小" | :208 记账 | `test_worst_case_size_bound_all_short_strings`、`test_real_payload_shrinks_dramatically` |

规则加载与兼容性由另一组测试守：`test_loader_accepts_scrub_paths_and_deprecated_remove_paths`、
`test_loader_fails_loud_on_invalid_path`、`test_loader_rejects_too_many_tokens`、
`test_loader_missing_file_is_not_fatal`、`test_loader_empty_and_broken_yaml_are_not_fatal`、
`test_loader_skips_rules_missing_fields`、`test_repo_rules_file_loads_with_scrub_paths`。
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

## 3. 三条执行路径

LiteLLM 可能以三种方式触发日志回调，三条路径必须语义一致：

1. `async_logging_hook` :472 —— 主路径（proxy 常态）。
2. `logging_hook` :550 —— 同步入口。事件循环已在跑 → 直接走 3；没在跑 →
   `run_until_complete` 跑 1；拿不到循环（≥3.12 无线程内 loop 等）→ 记录 WARNING 后退回 3。
3. `_sync_scrub` :584 —— 纯同步兜底，只清洗 `standard_logging_object["response"]`。

清洗只作用于 `standard_logging_object["response"]`，因为经核实 SpendLogs 的 `response`
正是取自这里；`result` 是已返回给客户端的对象且与之不共享引用，改动它不仅无收益，还会让
下游回调（如 `_ProxyDBLogger` 的 `hasattr(result, "choices")` 判断）静默失效。

---

## 4. 可观测性的真实情况

**默认部署下本模块的 INFO 日志不可见。** proxy 未把 root logger 调到 INFO 时只有
WARNING/ERROR 输出。因此安全类信号必须（也确实）用 WARNING：

- 命中规则却零清洗：`scrub rule matched but replaced nothing (rule prefix=…, paths=…, call_id=…)`
- 超出限深：`scrub stopped at N container(s) beyond MAX_SCRUB_DEPTH=64; their string leaves were NOT replaced`
- 规则文件缺失：`scrub rules file not found at …; scrubbing disabled`
- 路径语法非法 / 记号过多：启动期 `logger.error`
- 旧键名：`scrub rule uses deprecated key 'remove_paths'; rename to 'scrub_paths'`

实测留痕：把规则前缀改成不可能命中制造"零清洗"场景时，容器日志里确实打出上述 WARNING
原文。逐请求的清洗条数统计是 INFO，需要显式放开日志级别才看得到。

---

## 5. 已接受风险（明知有代价，不作为缺陷反复提出）

1. **结构即侧信道**：入库数据保留原始键名与每层数组长度。选方案 D 等于接受这一点——
   攻击者能推断响应形状，但拿不到内容与长度量级（占位符不带元信息）。
2. **体积不再是 O(1)**：净开销随字符串叶子数线性变化，且极短字符串被替换后可能变大
   （C16）。因此性能测试是发布门禁的一部分，不是可选项：1 万元素数组、5000 层深嵌套、
   1.5 MB 真实形状载荷的 hook 耗时 < 50 ms 都已入测试。
3. **规则不热更新**：`_RULES` 在模块导入时构建（:373），改文件必须重启容器。陈旧规则
   会 fail-open（数据原样入库）但有 WARNING 兜底。

---

## 6. 验收证据（2026-10-17 实测，全部可复跑）

| 验收项 | 证据 |
|---|---|
| 结构完整性 | 库内同载荷「清洗 / 未清洗」两行：键路径 20 vs 20、集合全等、值类型映射全等、差异路径为空 |
| 数据库侧 | 命中行 454 B 且 `base64残留=false`；未命中对照行 570 B 且残留=true；`jsonb_typeof=object`，键集 `id,model,usage,object,choices,created,system_fingerprint` |
| 体积 | 非流式 570→454、流式 748→632，均为 −116 = `len(content) 126 − len("<scrubbed>") 10`，与 C16 公式吻合 |
| 不泄露 | 大载荷轮（config 临时换成 11902 字符 base64）：清洗行 0/990 个片段泄露，对照行 167/990 |
| 客户端零影响（非流式） | 清洗开 / 关 / 再开三态响应体（剔除 `id`/`created`）sha256 均为 `2f7685d5efbe200f…197db1b` |
| 客户端零影响（流式） | 三态 SSE 各 8039 B / 43 chunk，归一化 sha256 均为 `24a514a1fc68edb6…db35a7dc`，重组 content 三态都是完整 base64 |
| 门禁 | 69 passed；`custom_logger.py` 207 stmts / 0 miss / 94 branch / 0 partial = 100%；变异 16/16 killed（控制组先须全绿）；ruff 0.17.1（E,W,F,SIM,RUF，排除中文全角标点误报 RUF001/002/003）全绿 |

未纳入门禁：**mypy**（钉死镜像与 `/deps` 均无该工具，未为此拉第二套工具链）。
`tests/` 里 `test_custom_logger.py` 是 pytest 套件；鉴权那个脚本式测试已随功能移出项目。

---

## 7. 复跑指南

### 7.1 单测 / 覆盖 / lint / 变异

门禁必须在钉死的镜像内跑（`litellm` 是真实导入，不用 stub）。依赖是跨平台下载的
linux cp313 wheel 解包目录，bind-mount 到 `/deps`：

```sh
export MSYS2_ARG_CONV_EXCL='*'          # Windows/MSYS 下必须，否则 /work 会被改写成盘符路径
docker run --rm -v "$PWD:/work" -v <deps>:/deps -w /work -e PYTHONPATH=/deps \
  -e SCRUB_RULES_PATH=/work/scrub_rules.yaml -e HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hyp \
  --entrypoint /app/.venv/bin/python3 ghcr.io/berriai/litellm:v1.83.14-stable \
  -m pytest tests/test_custom_logger.py -q --no-header -p no:cacheprovider \
  --cov=custom_logger --cov-report=term-missing --cov-branch
```

ruff 用 manylinux wheel 解包出的二进制（进容器需 `chmod +x`）；变异测试脚本对
`custom_logger.py` 施加 16 条语义变异，每条锚定上表一行契约，**锚点要求在源码中唯一**，
所以改注释后必须重验锚点。

### 7.2 端到端 A/B（客户端逐字节）

单代理串行跑，**不要并发起两个代理容器**（实测会把 Docker Desktop 引擎打到所有 API 返回 500）：

1. 校验 `scrub_rules.yaml` 的 sha256 是仓库原版（当前 `b4ac8a3829f70aa3…b20e77c3`）。
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
