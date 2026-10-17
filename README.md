# litellm-scrub — LiteLLM 代理响应体字段清洗

按 `api_base` 前缀，在**写入数据库之前**把响应体里的敏感/超大字段（典型是 base64 图片、
音频）替换成短占位符 `<scrubbed>`，同时**完整保留 JSON 结构**。客户端收到的响应不受任何
影响——被改的只有落库的那份副本。

## 为什么需要它

LiteLLM 会把每次调用的响应写进 Postgres 表 `LiteLLM_SpendLogs.response`（jsonb 列）。
v1.83.14 自带的兜底只按字符串截断（头 35% + 尾 65%，阈值 `MAX_STRING_LENGTH_PROMPT_IN_DB`
默认 2048），于是：一段 base64 仍有 1300+ 字符原样留在库里，且一行的体积没有上界。
本项目把目标字段的所有字符串叶子换成占位符，同时因为 jsonb 由数据库校验合法性，
**结构守恒是写入能否成功的硬约束**，不是风格选择。

## 快速开始

```sh
docker compose up -d                 # 起代理（镜像钉死 v1.83.14-stable，端口 4000）
curl -s -X POST http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-1234" -H "Content-Type: application/json" \
  -d @req_hit.json                   # 命中清洗规则的端到样例
```

端口刻意只绑回环（`127.0.0.1:4000:4000`）：这个代理带着 master key 和宿主机 Postgres
的凭据，不该默认对整个局域网开放。需要从别的机器访问时改 `docker-compose.yml`，
并自行确认网络边界。

`req_hit.json`（`hit-model` → `https://provider-a.example.com/v1`，命中规则）与
`req_miss.json`（`miss-model` → `https://provider-x.example.com/v1`，未命中）是自带的
两个冒烟样例：前者入库响应体变成 `<scrubbed>`，后者原样入库。

**规则只在容器启动时读一次**，改 `scrub_rules.yaml` 必须 `docker compose restart` 才生效。

## 项目结构

```
litellm-scrub/
├── custom_logger.py            生产代码，唯一一份：LiteLLM CustomLogger 回调
├── scrub_rules.yaml            清洗规则（:ro 挂进容器），文件头即路径语法说明
├── config.yaml                 代理配置：两个 mock 模型 + 挂载回调实例
├── docker-compose.yml          部署：钉死镜像版本、三个 :ro 挂载、可调环境变量
├── req_hit.json                冒烟样例：api_base 命中规则
├── req_miss.json               冒烟样例：不命中（对照组）
├── tests/
│   ├── test_custom_logger.py   82 条 pytest：结构守恒 / 不泄露 / 幂等 / 两个钩子
│   │                           路径 / 规则加载与匹配 / 性能与限深
│   └── mut_harness.py          22 条语义变异（脚本，不是 pytest 用例）
├── docs/
│   └── response-body-scrub.md  契约表 C1-C20、一手取证、已接受风险、复跑指南
├── .gitattributes              锁定 LF：源码以 :ro 直接挂进 Linux 容器，
│                               检出时被转成 CRLF 会污染运行时读到的字节
└── .gitignore
```

**没有** `requirements.txt`、也没有打包成 Python 包：运行时依赖全部来自钉死的
`ghcr.io/berriai/litellm:v1.83.14-stable` 镜像，`custom_logger.py` 只用到标准库 + 镜像里
已有的 `yaml`。测试依赖（pytest / hypothesis / jsonschema / coverage）不在仓库里，
跑门禁时从外部 bind-mount 到 `/deps`，见「验证」与 `docs` §7.1。

### `custom_logger.py` 的内部分层

自上而下读就是"一次请求怎么被清洗"的顺序，依赖只往上指：每一层只调用它上面那些层的成员，
所以想改清洗语义只需要动「清洗」层，想改命中范围只需要动「规则」层。

| 层 | 成员 | 职责 |
|---|---|---|
| 配置 | `_int_env`、`RULES_PATH`、`SCRUB_PLACEHOLDER`、`MAX_SCRUB_DEPTH`、`MAX_PATH_TOKENS` | 读环境变量；非法值告警后退回默认，绝不让 import 失败 |
| 路径 | `_tokenize`、`_KEY` / `_IDX` / `_WILD`、`_normalize_base` | 把 `choices[*].message.content` 这类字符串编译成记号列表；api_base 归一化 |
| 规则 | `_load_rules`、`_RULES`、`_match_rule` | 启动时一次性加载并预编译；最长前缀优先 + 边界校验 |
| 清洗 | `_scrub_subtree`、`_scrub_value`、`_scrub_path`、`_scrub_slo_response` | 显式栈遍历、限深、就地替换字符串叶子，返回 `(replaced, capped, saved)` |
| 上下文 | `_as_dict`、`_extract_api_base` | 从 LiteLLM 交进来的 `kwargs` 里按优先级找 api_base，任意一层形状不对都不抛异常 |
| 编排 | `_scrub_and_report`、`Base64ScrubLogger`、`base64_scrub_logger_instance` | 匹配 → 清洗 → 留痕；两个钩子共用这一份实现 |

### 一次请求的数据流

```
客户端 ──POST /v1/chat/completions──> LiteLLM 代理 ──> 上游 provider
   ^                                        │
   └────────── 响应原样返回（未被触碰）──────┘
                                            │  响应已发出之后
                                            v
                            async_logging_hook(kwargs, result)
                                            │
                        _extract_api_base(kwargs)  →  _match_rule(api_base)
                                            │  未命中：原样返回，一个字节都不改
                                            v
              就地清洗 kwargs["standard_logging_object"]["response"]
                                            │
                          返回 (kwargs, result)   ← result 刻意原样透传
                                            │
                                            v
              LiteLLM 从 slo["response"] 组装 SpendLogs 行 → 字符串截断兜底 → Postgres
```

清洗点在"响应已经返回给客户端之后"，且只改 `standard_logging_object["response"]`
（SpendLogs 的 `response` 字段正是取自这里），所以落库的是清洗后的内容、客户端拿到的
始终是原始内容。两条钩子路径（`litellm_logging.py:2670` 的 await 与 `:2104` 的同步调用）
都必须实现，漏掉任何一条等于在那条路径上完全不清洗。

## 配置项

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `SCRUB_RULES_PATH` | `/app/scrub_rules.yaml` | 规则文件路径；文件缺失 = 禁用清洗（启动时 WARNING） |
| `SCRUB_PLACEHOLDER` | `<scrubbed>` | 字符串叶子替换成的占位符，不携带长度/类型/hash |
| `MAX_SCRUB_DEPTH` | `64` | 子树遍历限深，超出则截停并报 WARNING（不静默漏清洗） |
| `MAX_PATH_TOKENS` | `64` | 单条路径记号数上限，启动时挡掉畸形路径 |

`MAX_SCRUB_DEPTH` / `MAX_PATH_TOKENS` 写了非法值（非整数或负数）时不会让回调加载失败，
而是打一条 WARNING 后退回默认值——compose 里一个拼写错误不该导致整个日志管道起不来。

路径语法：`key.subkey`、`key[*]`（数组每项）、`key[0]`（固定下标），支持 `[-1]`。
匹配为**最长前缀优先**，比较前双边归一化（小写、去尾部 `/`），并要求前缀之后是
`/ : ? #` 或结尾，避免 `provider-a.example.com.evil.com` 这类异域名误命中。
旧键名 `remove_paths` 仍可用，但启动时打 deprecation WARNING，建议改成 `scrub_paths`。
**同一前缀不要写两条规则**：命中即返回，第二条的 `scrub_paths` 永远不会被执行，
启动期会打 ERROR 提示合并。

## 验证

```sh
export MSYS2_ARG_CONV_EXCL='*'          # Windows/MSYS 下必须
IMG=ghcr.io/berriai/litellm:v1.83.14-stable
PY=/app/.venv/bin/python3

# 单测 + 分支覆盖（在钉死的镜像内跑，litellm 是真实导入，无 stub）
docker run --rm -v "$PWD:/work" -v <deps>:/deps -w /work -e PYTHONPATH=/deps \
  -e SCRUB_RULES_PATH=/work/scrub_rules.yaml -e HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hyp \
  --entrypoint $PY $IMG \
  -m pytest tests/test_custom_logger.py -q --cov=custom_logger --cov-branch

# 变异门禁：证明测试写错实现时会红（22 条，锚点必须在源码中唯一）
docker run --rm -v "$PWD:/work" -v <deps>:/deps -w /work -e PYTHONPATH=/deps \
  -e SCRUB_RULES_PATH=/work/scrub_rules.yaml -e HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hyp \
  --entrypoint $PY $IMG tests/mut_harness.py
```

当前状态：**82 passed，`custom_logger.py` 219 语句 / 102 分支 100% 覆盖，
22/22 语义变异全杀，ruff 0.17.1（E,W,F,SIM,RUF，排除全角标点误报 RUF001/002/003）无错误**。
1.43 MiB 真实形状载荷整条 hook 2.2 µs、1 万元素宽数组 4.90 ms、5000 层深链 11.6 µs。
`<deps>` 是跨平台下载的 linux cp313 wheel 目录；复跑取证的完整命令、工具链位置与
数据库侧断言 SQL 见 `docs/response-body-scrub.md`。mypy 未纳入门禁（镜像与依赖里都没有）。

## 边界与已知取舍

- **只处理响应体**。请求体的超长内容由上游 `spend_tracking_utils.py:809` 的截断兜底。
- **未命中前缀的请求原样入库**——这是设计，不是漏清洗；全表扫描看到 base64 字样需按
  规则命中范围判断。
- 命中规则却一个叶子都没换 → WARNING（`scrub rule matched but replaced nothing`），
  数据仍原样入库（fail-open）。清洗失效一定会留下可见痕迹，不会静默。
- **只管入库，不管容器日志**：`--detailed_debug` 会让 LiteLLM 以 DEBUG 打出
  `RAW RESPONSE`（`litellm_logging.py:1231`），把清洗前的整段 base64 原样写进
  `docker logs`。所以 compose 里刻意没有这个开关，排查时临时加、用完删。
- 结构本身是侧信道：入库数据保留原始键名与每层数组长度。接受理由见
  `docs/response-body-scrub.md` 的「已接受风险」。

## 仓库状态

本仓库于 2026-10-17 建立 git 基线后随即收敛：自定义鉴权功能
（`custom_auth.py` / `auth_config.yaml` / 对应测试与设计文档）已移出仓库，当前历史里
从未出现过这些文件，仓库只保留响应体清洗。两份副本的位置：
`C:/Users/lqw/AppData/Local/Temp/litellm-scrub-quarantine/`（系统 TEMP，可能被清理）
与 `C:/Users/lqw/Desktop/工作/project/模型权限认证/_from_litellm-scrub_baseline/`。
需要找回鉴权功能：从上面任一目录拷回，
并恢复 `config.yaml` 的 `general_settings.custom_auth` 与 compose 里的两条挂载。
