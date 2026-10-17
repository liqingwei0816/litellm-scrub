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

`req_hit.json`（`hit-model` → `https://provider-a.example.com/v1`，命中规则）与
`req_miss.json`（`miss-model` → `https://provider-x.example.com/v1`，未命中）是自带的
两个冒烟样例：前者入库响应体变成 `<scrubbed>`，后者原样入库。

**规则只在容器启动时读一次**，改 `scrub_rules.yaml` 必须 `docker compose restart` 才生效。

## 文件

| 文件 | 作用 |
|---|---|
| `custom_logger.py` | 清洗实现（LiteLLM `CustomLogger` 回调），逐行中文注释 |
| `scrub_rules.yaml` | 规则：`api_base_prefix` + `scrub_paths`，文件头即是路径语法与语义说明 |
| `config.yaml` | 代理配置：两个 mock 模型 + 挂载回调 `custom_logger.base64_scrub_logger_instance` |
| `docker-compose.yml` | 部署与可调环境变量 |
| `tests/test_custom_logger.py` | 69 条测试（结构守恒 / 不泄露 / 幂等 / 三条执行路径 / 性能与限深 / 变异补齐） |
| `docs/response-body-scrub.md` | 契约表、取证、已接受风险、验收与复跑命令 |

## 配置项

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `SCRUB_RULES_PATH` | `/app/scrub_rules.yaml` | 规则文件路径；文件缺失 = 禁用清洗（启动时 WARNING） |
| `SCRUB_PLACEHOLDER` | `<scrubbed>` | 字符串叶子替换成的占位符，不携带长度/类型/hash |
| `MAX_SCRUB_DEPTH` | `64` | 子树遍历限深，超出则截停并报 WARNING（不静默漏清洗） |
| `MAX_PATH_TOKENS` | `64` | 单条路径记号数上限，启动时挡掉畸形路径 |

路径语法：`key.subkey`、`key[*]`（数组每项）、`key[0]`（固定下标），支持 `[-1]`。
匹配为**最长前缀优先**，比较前双边归一化（小写、去尾部 `/`），并要求前缀之后是
`/ : ? #` 或结尾，避免 `provider-a.example.com.evil.com` 这类异域名误命中。
旧键名 `remove_paths` 仍可用，但启动时打 deprecation WARNING，建议改成 `scrub_paths`。

## 验证

```sh
# 单测 + 分支覆盖（在钉死的镜像内跑，litellm 是真实导入，无 stub）
docker run --rm -v "$PWD:/work" -v <deps>:/deps -w /work -e PYTHONPATH=/deps \
  -e SCRUB_RULES_PATH=/work/scrub_rules.yaml -e HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hyp \
  --entrypoint /app/.venv/bin/python3 ghcr.io/berriai/litellm:v1.83.14-stable \
  -m pytest tests/test_custom_logger.py -q --cov=custom_logger --cov-branch
```

当前状态：**69 passed，`custom_logger.py` 207 语句 / 94 分支 100% 覆盖，
16/16 语义变异全杀，ruff（E,W,F,SIM,RUF）无错误**。
`<deps>` 是跨平台下载的 linux cp313 wheel 目录；复跑取证的完整命令、工具链位置与
数据库侧断言 SQL 见 `docs/response-body-scrub.md`。mypy 未纳入门禁（镜像与依赖里都没有）。

## 边界与已知取舍

- **只处理响应体**。请求体的超长内容由上游 `spend_tracking_utils.py:809` 的截断兜底。
- **未命中前缀的请求原样入库**——这是设计，不是漏清洗；全表扫描看到 base64 字样需按
  规则命中范围判断。
- 命中规则却一个叶子都没换 → WARNING（`scrub rule matched but replaced nothing`），
  数据仍原样入库（fail-open）。清洗失效一定会留下可见痕迹，不会静默。
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
