"""
====================================================================
LiteLLM 自定义日志器（响应体字段清洗：字符串叶子替换为占位符）
====================================================================

【这个文件是做什么的？】
LiteLLM 是一个"LLM 网关/代理"，它会把每次调用大模型的请求和响应
记录到数据库表 LiteLLM_SpendLogs 里（用于计费、审计）。

问题：有些供应商会在响应里返回超大的 base64 图片/音频数据。如果原样存进
数据库，会导致：
  1) 单行数据巨大，拖慢数据库；
  2) 敏感载荷残留。经核实 LiteLLM v1.83.14 源码：入库前每个超长字符串都会被
     "按字符串"截断（litellm/proxy/spend_tracking/spend_tracking_utils.py:884
     调 _sanitize_request_body_for_spend_logs_payload，:665-696 是实现），保留
     头 35% + 尾 65%，阈值 MAX_STRING_LENGTH_PROMPT_IN_DB 默认 2048
     （litellm/constants.py:387）。它只改字符串内容、不改 JSON 结构，所以
     "截断会弄坏 JSON" 这个说法对本版本并不成立；真正的问题是：一段 base64
     里会有 1300 多个字符被原样留在库里，而且一行里有多少个超长字符串、每个
     都留 2048 字符，行体积是没有上界的。

本文件的作用：在日志"写入数据库之前"，根据请求来源（api_base）把响应体里
配置好的敏感/超大字段，替换成一个很短的占位符字符串，让入库的 JSON 既精简
又结构完整。关键点：只改"日志里存的那份"，客户端收到的响应完全不受影响。

【核心规则】
- 规则文件：启动时从 SCRUB_RULES_PATH（默认 /app/scrub_rules.yaml）
  读取一次。没有"热更新"——改了规则文件必须重启容器才生效。
- 匹配方式：按 api_base 的"前缀"匹配，前缀越长优先级越高；
  比较前双方都会"归一化"（转小写、去掉结尾的斜杠 /）。
- 路径写法：用"点路径 + 数组下标"定位要清洗的字段，例如：
      choices[*].message.images   处理 choices 数组里每个元素的 message.images
      data[*].b64_json            处理 data 数组里每个元素的 b64_json
      choices[0].message.content  只处理 choices 第 0 个元素的 message.content
  其中 [*] 表示"数组里的每一项"，[0] 表示"第 0 项"。
  如果某个路径在实际数据里对不上（结构不符），会被安静地跳过，不报错；
  但"规则命中却一个都没换"会记录 warning，避免规则写错或过期后静默漏清洗。
- 替换语义（结构完全守恒）：命中的字段不再"删除键"，而是把它下面**所有字符串
  叶子**原地替换成占位符 SCRUB_PLACEHOLDER。键名、值的 JSON 类型、每一层数组的
  元素个数、嵌套结构全部与原始一致——只有字符串叶子的"值"变了。
  * 非字符串叶子（int / float / bool / null）原样保留：给它们赋字符串会破坏
    类型守恒，而真实的 base64/敏感载荷一律是字符串。
  * 空容器（[] 与 {}）原样保留（没有字符串叶子可换）。
  * 幂等：占位符本身也是字符串叶子，再跑一遍只会写回同一个值，不会套娃。
  * 占位符不携带任何元信息（无原长度、无类型、无 hash），避免把"内容指纹"
    写进数据库；量级信息走日志。实测：默认部署（proxy 未把 root logger 调到
    INFO）只有 WARNING/ERROR 会输出，所以"命中规则却零清洗""超出限深"这类
    必须被看见的事件用的是 warning；逐请求的 info 统计需要显式放开级别。
  * 体积上界：净增量 ≤ Σ max(0, len(占位符) - len(原串))。若目标子树里有比
    占位符更短的字符串（例如空串），那一处清洗后反而会变大，单个叶子最多
    大 len(占位符) 个字符（空串时取等号）；真实 base64 载荷远长于占位符，
    整体必然下降。不承诺"任何载荷都变小"，只承诺"增量有上界"。
  遍历方式与上游 litellm/litellm_core_utils/logging_utils.py:78-124
  （_truncate_base64_in_value）一致：显式栈、限深、只动字符串、保键与数组长度。
- 作用范围：只处理"响应体"。上游对"请求体"已有自己的截断兜底
  （spend_tracking_utils.py:809），本文件不重复处理。
- 容错：本文件任何一处出错，都会被捕获并记录为 warning，然后让日志
  "原样写入"。绝不因为清洗逻辑的 bug 而弄坏整个日志流程或请求本身。
====================================================================
"""

# --------------------------------------------------------------------
# 导入依赖（import 相当于"引入别人写好的工具包"）
# --------------------------------------------------------------------
import logging    # Python 标准日志库：用来打印 warning / info 等日志
import os         # 操作系统相关：这里用来读取环境变量
import re         # 正则表达式：用来按规则匹配/拆分字符串

import yaml       # 第三方库：用来读取 .yaml 配置文件（规则文件就是 yaml 格式）

# 从 LiteLLM 官方提供的基类 CustomLogger 继承，
# 这样 LiteLLM 才会把我们的类当作"日志回调"来调用。
from litellm.integrations.custom_logger import CustomLogger

# 创建一个本模块专用的"日志记录器"，名字叫 base64_scrub_logger。
# 后续所有 logger.warning(...) / logger.info(...) 都会带上这个名字，便于排查。
logger = logging.getLogger("base64_scrub_logger")

# 规则文件路径：优先读环境变量 SCRUB_RULES_PATH，没设置就用默认值。
# os.environ.get(键, 默认值) 的意思是：环境变量里有就用它，没有就用默认值。
RULES_PATH = os.environ.get("SCRUB_RULES_PATH", "/app/scrub_rules.yaml")

# 占位符：所有被清洗的字符串叶子都替换成这个短串。
# 刻意不带任何元信息（不写原长度、原类型、hash），因为精确长度等于给内容
# 做了个指纹。字面量用尖括号包裹：base64 与正常文本里都不会出现这个形状，
# 便于"确认原值已彻底消失"的扫描，也便于人工在库里一眼识别。
SCRUB_PLACEHOLDER = os.environ.get("SCRUB_PLACEHOLDER", "<scrubbed>")

# 子树遍历的最大深度。超过就停在那里不再往里走（并计入 capped 告警），
# 目的有两个：一是防畸形超深载荷把遍历时间拉长，二是让"清洗没覆盖到"
# 变成一个可观测事件而不是静默漏清洗。真实 LLM 响应通常在 3-4 层
# （上游同机制取 20，见 logging_utils.py:48 _MAX_TRUNCATION_DEPTH），
# 这里留足余量。
MAX_SCRUB_DEPTH = int(os.environ.get("MAX_SCRUB_DEPTH", "64"))

# 单条路径允许的记号数量上限，用于在启动时就挡掉畸形超长路径。
MAX_PATH_TOKENS = int(os.environ.get("MAX_PATH_TOKENS", "64"))

# --------------------------------------------------------------------
# 路径 token（记号）的三种类型常量
# --------------------------------------------------------------------
# 我们把一条路径字符串（如 choices[*].message）拆成一串"记号(token)"，
# 每个记号代表"下一步该怎么走"。这里定义三种记号类型：
#   _KEY  = "key"   表示"进入某个键"，例如 message 表示进入 message 这个字段
#   _IDX  = "idx"   表示"进入数组的某个固定下标"，例如 [0] 表示第 0 个元素
#   _WILD = "wild"  表示"通配数组里的每一项"，即 [*]
_KEY, _IDX, _WILD = "key", "idx", "wild"

# 两个"正则表达式"（一种描述字符串模式的微型语言），用于拆分路径：
# _PART_RE：匹配路径中被点号(.)分隔的"一段"，例如 choices[*] 这一段。
#           它把这一段再拆成两部分：前面的名字(choices) 和 后面的方括号部分([*])。
# _IDX_RE ：专门用来从方括号部分里，把每个 [xxx] 中的 xxx 抠出来。
#           例如 [*][0] 会依次抠出 "*" 和 "0"。
_PART_RE = re.compile(r"^([^\[\]]*)((?:\[[^\[\]]+\])*)$")
_IDX_RE = re.compile(r"\[([^\[\]]+)\]")


def _normalize_base(base: str) -> str:
    """
    把一个 api_base 字符串"归一化"，让比较更稳健。
    做三件事：
      1) strip()  去掉首尾的空白字符（空格、换行等）
      2) lower()  全部转成小写（这样 HTTP:// 和 http:// 视为相同）
      3) rstrip("/") 去掉结尾的斜杠（这样 .../v1 和 .../v1/ 视为相同）
    """
    return base.strip().lower().rstrip("/")


def _tokenize(path: str):
    """
    把一条路径字符串解析成"记号列表"。

    举例：输入 'choices[*].message.images'
    先按点号(.)切成三段：'choices[*]'、'message'、'images'
    再逐段解析：
        'choices[*]' -> 名字 choices（一个 KEY 记号）+ [*]（一个 WILD 记号）
        'message'    -> 一个 KEY 记号
        'images'     -> 一个 KEY 记号
    最终得到：[('key','choices'), ('wild',None), ('key','message'), ('key','images')]

    返回值：
        成功 -> 记号列表（一个 Python 列表，元素是 (类型, 值) 的二元组）
        路径格式非法 -> 返回 None（表示"这条路径写错了，别用它"）
    """
    tokens = []                      # 用来收集解析出来的记号
    for part in path.split("."):     # 按点号把路径切成一段段，逐段处理
        m = _PART_RE.match(part)     # 用正则去匹配这一段
        if not m:                    # 匹配不上说明这段写法非法
            return None              # 直接判定整条路径非法
        name, brackets = m.group(1), m.group(2)  # 取出"名字部分"和"方括号部分"
        if name:                     # 如果有名字（比如 choices、message）
            tokens.append((_KEY, name))          # 记一个 KEY 记号
        for raw in _IDX_RE.findall(brackets):    # 把方括号里的内容一个个抠出来
            if raw == "*":           # 如果是 *，表示"数组每一项"
                tokens.append((_WILD, None))     # 记一个 WILD 记号（没有具体值）
            else:                    # 否则应该是一个数字下标，比如 0、1
                try:
                    tokens.append((_IDX, int(raw)))  # 转成整数，记一个 IDX 记号
                except ValueError:   # 转不成整数（比如写了 [abc]）说明非法
                    return None
    # tokens or None：如果列表为空（空路径），返回 None；否则返回列表本身
    return tokens or None


def _scrub_subtree(container):
    """
    就地遍历 container（一个 dict 或 list）的**整棵子树**，把里面所有
    "字符串叶子"替换成 SCRUB_PLACEHOLDER。
    返回 (替换掉的叶子数, 因超过深度上限而被截停的容器数, 净节省的字符数)。

    【结构守恒】
    只改字符串叶子的"值"：键名一个都不删、每个值的 JSON 类型都不变、
    每个数组的元素个数都不变。所以下游拿这份 JSON 时，凡是按键路径、
    按数组下标、按类型访问的地方都不会因为我们的清洗而报错。

    【为什么用显式栈而不是递归】
    载荷可能非常深，递归会撞上 Python 默认 1000 层的调用栈上限并抛
    RecursionError。改成手工维护栈之后，遍历深度只受 MAX_SCRUB_DEPTH
    约束，不存在栈溢出。（上游同机制见 logging_utils.py:78-124。）

    【幂等】
    占位符本身也是字符串叶子，再遍历一次只会写回同一个值；这里特意不
    把"已经是占位符"计入 replaced，所以同一份数据被处理多次时，计数不会
    虚高，日志里看到的数字始终是"这次真正改了多少"。

    【非字符串叶子不动】
    int / float / bool / None 一律原样保留。给它们赋字符串会破坏上面承诺的
    类型守恒，而真实的 base64、图片、音频这类大载荷在响应里都是字符串。
    """
    replaced = 0
    capped = 0
    saved = 0
    # 栈里放"还没走完的容器"和它所在的深度
    stack = [(container, 0)]
    while stack:
        node, depth = stack.pop()
        # dict 用它的键值对，list 用 (下标, 元素)；统一成同一种写法处理
        items = node.items() if isinstance(node, dict) else list(enumerate(node))
        for key, value in items:
            if isinstance(value, str):
                # 只改这个键/下标上的值，不增不减键、不增不减元素，
                # 所以遍历过程中修改 node 是安全的（字典大小没变）。
                if value != SCRUB_PLACEHOLDER:
                    node[key] = SCRUB_PLACEHOLDER
                    replaced += 1
                    # 顺手记账：省了多少字符。这样调用方想知道体积收益时
                    # 不需要再 json.dumps 整个响应去测量。
                    saved += len(value) - len(SCRUB_PLACEHOLDER)
            elif isinstance(value, (dict, list)):
                if depth + 1 > MAX_SCRUB_DEPTH:
                    # 太深了：停在这里不再往里走，并把它记下来，让调用方
                    # 能在日志里看到"清洗没有完全覆盖"，而不是静默放过。
                    capped += 1
                    continue
                stack.append((value, depth + 1))
            # 其它类型：非字符串叶子，按契约原样保留
    return replaced, capped, saved


def _scrub_value(value):
    """
    清洗"路径终点上的那个值"。
    返回 (要写回去的新值, 替换掉的叶子数, 被截停的容器数, 净节省的字符数)。

    三种情况：
      1) 值是字符串  -> 返回占位符（最常见的叶子字段，如 content、b64_json）
      2) 值是容器    -> 进子树，把所有字符串叶子换掉，容器本身原样返回（就地已改）
      3) 值是其它    -> 原样返回，不动
    """
    if isinstance(value, str):
        if value == SCRUB_PLACEHOLDER:
            return value, 0, 0, 0
        return SCRUB_PLACEHOLDER, 1, 0, len(value) - len(SCRUB_PLACEHOLDER)
    if isinstance(value, (dict, list)):
        replaced, capped, saved = _scrub_subtree(value)
        return value, replaced, capped, saved
    return value, 0, 0, 0


def _scrub_path(obj, tokens):
    """
    根据"记号列表"下钻到目标位置，就地清洗那里的值。
    返回 (替换掉的叶子数, 被截停的容器数, 净节省的字符数)。

    【终点做什么】
    走到最后一个记号时执行 _scrub_value：键还在、值的类型不变、数组长度不变，
    只有字符串值被换成占位符。

    【容错】
    如果中途发现实际数据结构和路径对不上（比如路径要进 message，
    但当前这层根本没有 message 这个键），就安静地返回全 0，不报错。
    这样即使规则和真实响应结构不完全一致，也不会崩溃。
    """
    if not tokens:               # 记号列表为空：没有要清洗的东西
        return 0, 0, 0
    kind, val = tokens[0]        # 取出"第一个记号"
    rest = tokens[1:]            # "去掉第一个后的剩余记号"
    terminal = not rest          # 是不是最后一个记号（是的话终点就该动手了）

    if kind == _KEY:             # 当前记号是"进入某个键"
        # obj 不是字典，或字典里没有这个键 -> 结构对不上，安静返回
        if not isinstance(obj, dict) or val not in obj:
            return 0, 0, 0
        if terminal:
            obj[val], n, c, s = _scrub_value(obj[val])
            return n, c, s
        return _scrub_path(obj[val], rest)

    elif kind == _IDX:           # 当前记号是"进入数组的某个固定下标"
        # obj 必须是列表，且下标 val 在合法范围内（支持负数下标，-1 表示倒数第一个）
        if not isinstance(obj, list) or not (-len(obj) <= val < len(obj)):
            return 0, 0, 0
        if terminal:
            # 原地替换这一项（不弹出）：数组长度守恒
            obj[val], n, c, s = _scrub_value(obj[val])
            return n, c, s
        return _scrub_path(obj[val], rest)

    elif kind == _WILD:          # 当前记号是"通配数组里的每一项"（[*]）
        if not isinstance(obj, list):   # 当前这层不是数组 -> 对不上，安静返回
            return 0, 0, 0
        n_total = c_total = s_total = 0
        if terminal:
            # 逐项清洗，每一项都保持原来的形状，数组长度守恒
            for i in range(len(obj)):
                obj[i], n, c, s = _scrub_value(obj[i])
                n_total += n
                c_total += c
                s_total += s
        else:                    # 否则：对数组里的"每一项"都递归处理后续记号
            for item in obj:
                n, c, s = _scrub_path(item, rest)
                n_total += n
                c_total += c
                s_total += s
        return n_total, c_total, s_total

    return 0, 0, 0               # 未知记号类型（不会发生，保险起见）


def _load_rules(path: str):
    """
    从 yaml 文件加载规则，并"预编译"成程序内部好用的结构。

    【为什么要预编译？】
    规则文件里写的是人类可读的路径字符串（如 "choices[*].message.content"）。
    每次请求都现解析字符串太浪费。所以在启动时一次性把它们解析成"记号列表"，
    之后每次请求直接拿现成的记号去清洗，速度快。

    【排序】
    加载完后按"前缀长度从长到短"排序。这样匹配时从头往后找，
    第一个命中的自然就是"最长前缀"，实现了"最长前缀优先"。

    返回值：一个列表，每个元素是 (归一化后的前缀, 该规则下的记号列表们)。
    """
    compiled = []                # 收集编译好的规则
    try:
        # 打开并读取 yaml 文件。with 语句保证文件用完会自动关闭。
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}   # 解析 yaml 成 Python 字典；空文件则用 {}
        # data.get("rules", [])：取出 rules 这个键对应的列表；没有就给空列表
        for rule in data.get("rules", []):   # 遍历每一条规则
            prefix = rule.get("api_base_prefix")     # 这条规则针对的 api_base 前缀
            # 主键名是 scrub_paths；旧键名 remove_paths 继续兼容并给出提醒。
            # 之所以不直接废弃旧键：规则文件是外部挂载卷，改了键名却忘了同步
            # 文件的话，结果是"静默不清洗"，比"清洗得不理想"危险得多。
            paths = rule.get("scrub_paths")
            if paths is None and rule.get("remove_paths") is not None:
                logger.warning(
                    "scrub rule uses deprecated key 'remove_paths'; "
                    "rename to 'scrub_paths' (prefix=%s)", prefix,
                )
                paths = rule.get("remove_paths")
            paths = paths or []
            if not prefix or not paths:              # 前缀或路径缺失 -> 这条规则无效
                logger.error("scrub rule skipped (missing fields): %s", rule)
                continue                             # 跳过这条，处理下一条
            token_lists = []                         # 收集本规则下所有路径的记号
            for p in paths:                          # 遍历每个要清洗的路径字符串
                tokens = _tokenize(str(p))           # 解析成记号列表
                if tokens is None:                   # 解析失败（路径写错了）
                    # error 而不是 warning：在替换语义下，一条写错的路径意味着
                    # 敏感字段会被整段原样写进数据库，必须在启动日志里显眼。
                    logger.error("invalid scrub_path skipped: %s", p)
                    continue                         # 跳过这个路径
                if len(tokens) > MAX_PATH_TOKENS:    # 路径深得不正常
                    logger.error(
                        "scrub_path skipped (too many tokens %d > %d): %s",
                        len(tokens), MAX_PATH_TOKENS, p,
                    )
                    continue
                # 保存 (原始路径字符串, 记号列表)。原始字符串留着主要用于日志。
                token_lists.append((str(p), tokens))
            if token_lists:                          # 这条规则至少有一个有效路径才收录
                # 前缀先归一化，再和它的记号列表们一起存起来
                compiled.append((_normalize_base(str(prefix)), token_lists))
        # 按前缀长度降序排序：len(r[0]) 取前缀长度，reverse=True 表示从大到小
        compiled.sort(key=lambda r: len(r[0]), reverse=True)
        # 打印一条 info 日志，说明加载了几条规则、分别是哪些前缀（便于确认生效）
        logger.info(
            "scrub rules loaded from %s: %d rule(s): %s",
            path, len(compiled), [c[0] for c in compiled],
        )
    except FileNotFoundError:    # 规则文件不存在：不算致命错误，只是"禁用清洗"
        logger.warning("scrub rules file not found at %s; scrubbing disabled", path)
    except Exception:            # 其它任何错误：同样不让程序崩，记录详细堆栈后禁用
        logger.exception("failed to load scrub rules from %s; scrubbing disabled", path)
    return compiled


# 模块被导入时（也就是容器启动时）立即加载一次规则，存到全局变量 _RULES。
# 之后所有请求都复用这份已编译好的规则，不会重复读文件。
_RULES = _load_rules(RULES_PATH)


def _match_rule(api_base):
    """
    拿一个 api_base 去和所有规则比对，找出命中的那条规则。

    因为 _RULES 已经按"前缀从长到短"排好序，所以从头往后遍历，
    第一个满足"api_base 以该前缀开头"的规则，就是最精确（最长前缀）的匹配。

    返回值：
        命中 -> (命中的前缀, 该规则的记号列表们)
        没命中 -> None（表示这个请求不需要清洗，原样写入）
    """
    # api_base 为空或不是字符串：无从匹配，返回 None
    if not api_base or not isinstance(api_base, str):
        return None
    normalized = _normalize_base(api_base)   # 先把待匹配的 api_base 也归一化
    for prefix, token_lists in _RULES:       # 按"长前缀优先"的顺序逐条比对
        if normalized.startswith(prefix):    # startswith：判断是否以 prefix 开头
            # 边界校验：前缀之后必须是"路径边界"（/ : ? #）或字符串结尾，
            # 否则会把 https://provider-a.example.com.evil.com 这种异域名误命中。
            rest = normalized[len(prefix):]  # 取出前缀之后剩余的部分
            if rest == "" or rest[0] in ("/", ":", "?", "#"):
                return prefix, token_lists   # 命中，立即返回
    return None                              # 全部比完都没命中


def _extract_api_base(kwargs) -> str:
    """
    从一次请求的"上下文数据" kwargs 里，尽力找出这次请求的 api_base。

    【为什么要在好几个地方找？】
    LiteLLM 在不同版本、不同调用方式下，会把 api_base 放在不同的位置。
    我们按"最可靠 -> 兜底"的顺序逐个尝试，哪个先有值就用哪个。

    经实测（v1.83.x proxy 模式）：部署配置的 api_base 最可靠地存在于
    litellm_params.metadata.api_base（由路由器 router 写入，连 mock 调用都有）。
    其余几个位置作为兜底，兼容"直接用 SDK 调用"或未来版本的变化。

    参数 kwargs：一个字典，装着这次调用的各种上下文信息。
    返回值：找到的 api_base 字符串；都找不到就返回空字符串 ""。
    """
    # .get(键) or {} 的写法：取出这个键，如果它是 None 就用空字典 {} 代替，
    # 这样后面再 .get 就不会因为对 None 取值而报错。
    slo = kwargs.get("standard_logging_object") or {}      # 标准日志对象
    litellm_params = kwargs.get("litellm_params") or {}    # LiteLLM 内部参数
    metadata = litellm_params.get("metadata") or {}        # 路由器写入的元数据
    # 按优先级排列的候选位置，逐个尝试：
    for candidate in (
        # 1) 最可靠：路由器写入的 metadata.api_base（先确认 metadata 是字典再取）
        metadata.get("api_base") if isinstance(metadata, dict) else None,
        # 2) litellm_params 顶层的 api_base
        litellm_params.get("api_base"),
        # 3) 标准日志对象顶层的 api_base
        slo.get("api_base"),
        # 4) 标准日志对象里 model_parameters 下的 api_base
        (slo.get("model_parameters") or {}).get("api_base"),
        # 5) 标准日志对象里 metadata 下的 api_base
        ((slo.get("metadata") or {}) or {}).get("api_base"),
        # 6) 兜底：optional_params 里的 api_base
        (kwargs.get("optional_params") or {}).get("api_base"),
    ):
        if candidate:                # 只要这个候选值"非空"（不是 None/空串）
            return str(candidate)    # 就把它转成字符串返回
    return ""                        # 全都找不到，返回空串


def _scrub_slo_response(slo, token_lists):
    """
    对 standard_logging_object["response"] 按规则逐条清洗。
    返回 (替换掉的叶子数, 被截停的容器数, 净节省的字符数)。

    async_logging_hook 和 _sync_scrub 都调用这里，保证"主路径"和"兜底路径"
    的清洗语义、统计口径完全一致——否则一旦走了兜底，日志里的数字就不可比了。
    """
    # 双重确认：slo 是字典，且它的 response 也是字典，才动手
    if not (isinstance(slo, dict) and isinstance(slo.get("response"), dict)):
        return 0, 0, 0
    replaced = capped = saved = 0
    for _raw_path, tokens in token_lists:            # 遍历这条规则的每个路径
        # 直接传 tokens 即可：_scrub_path 只"读取"记号（tokens[0]、rest 切片），
        # 从不修改它，因此无需复制，省去每请求的开销。
        n, c, s = _scrub_path(slo["response"], tokens)
        replaced += n
        capped += c
        saved += s
    return replaced, capped, saved


class Base64ScrubLogger(CustomLogger):
    """
    自定义日志器主类。继承自 LiteLLM 的 CustomLogger。

    LiteLLM 在"一次请求成功、准备写日志"时，会调用我们重写的
    async_logging_hook 方法，把"上下文 kwargs"和"响应结果 result"交给我们，
    让我们有机会在写库前修改它们。我们就是在这里执行清洗。
    """

    async def async_logging_hook(self, kwargs, result, call_type):
        """
        异步日志钩子（主入口）。async 表示这是一个"异步函数"，
        LiteLLM 会在它的异步流程里等待(await)它执行完。

        参数：
            kwargs    ：这次调用的上下文字典（含 standard_logging_object 等）
            result    ：这次调用得到的响应结果对象
            call_type ：调用类型（如 acompletion 表示异步补全）
        返回：
            (kwargs, result) 这个二元组。LiteLLM 会拿我们返回的这两样继续往下走。
        """
        try:
            # 第一步：找出这次请求的 api_base
            api_base = _extract_api_base(kwargs)
            # 第二步：用 api_base 去匹配规则
            matched = _match_rule(api_base)
            if not matched:
                # 没命中任何规则：什么都不改，原样把 kwargs、result 透传回去
                return kwargs, result
            # 命中了：解包出"命中的前缀"和"这条规则要清洗的记号列表们"
            prefix, token_lists = matched

            # ---- 清洗点 1：standard_logging_object ----
            # 这是关键！LiteLLM 写 SpendLogs 表时，响应体实际取自
            # standard_logging_object["response"]。所以必须在这里原地改，
            # 数据库里存的才会是清洗后的内容。
            # 清洗主战场：standard_logging_object["response"]
            replaced, capped, saved = _scrub_slo_response(
                kwargs.get("standard_logging_object"), token_lists
            )

            # ---- 关于 result：刻意"原样透传"，不做任何修改 ----
            # 经核实 LiteLLM v1.83.x 源码：
            #   1) SpendLogs 表的 response 字段取自
            #      standard_logging_object["response"]，
            #      已在上面"清洗点 1"原地处理完毕，数据库里已是清洗后的内容；
            #   2) slo["response"] 是 LiteLLM 内部生成的独立 dict，与返回给客户端的
            #      响应对象没有共享引用；
            #   3) 本钩子在响应"已经返回给客户端之后"才被调用。
            # 因此完全不需要、也不应该替换 result。若把 result 换成 model_dump() 得到的
            # 裸 dict，会使下游回调（如 _ProxyDBLogger 的工具登记）用属性访问
            # hasattr(result, "choices") 判断失败，导致 tool_calls 静默丢失。
            # 所以这里直接返回原始 result，保持其对象类型不变。

            # 命中了规则却一个叶子都没换：几乎一定是规则路径写错或已经过期
            # （响应结构变了）。在替换语义下这意味着敏感数据被整段入库，
            # 所以这里显式提醒，而不是让它静默通过。
            if replaced == 0:
                logger.warning(
                    "scrub rule matched but replaced nothing "
                    "(rule prefix=%s, paths=%s, call_id=%s); "
                    "check that these paths still exist in the response",
                    prefix, [p for p, _ in token_lists],
                    kwargs.get("litellm_call_id"),
                )
            # 记录一条 info 日志，说明这次确实清洗了多少（便于排查与容量评估）
            logger.info(
                "scrubbed response for api_base=%s (rule prefix=%s, call_id=%s): "
                "%d string leaf/leaves replaced, ~%d chars saved, "
                "%d container(s) depth-capped",
                api_base, prefix, kwargs.get("litellm_call_id"),
                replaced, saved, capped,
            )
            if capped:
                logger.warning(
                    "scrub stopped at %d container(s) beyond MAX_SCRUB_DEPTH=%d; "
                    "their string leaves were NOT replaced (prefix=%s)",
                    capped, MAX_SCRUB_DEPTH, prefix,
                )
            # 返回：kwargs（其中的 standard_logging_object 已被原地改过）+ 原始 result
            return kwargs, result
        except Exception:
            # 最外层兜底：无论上面哪里出了意外，都绝不让日志管道崩溃。
            # 记录 warning（带完整堆栈 exc_info=True），然后原样返回，让日志照常写入。
            logger.warning("async_logging_hook failed; writing as-is", exc_info=True)
            return kwargs, result

    def logging_hook(self, kwargs, result, call_type):
        """
        同步版日志钩子（兜底用）。

        绝大多数情况下 LiteLLM 走的是上面的 async 版本。但极少数调用点
        可能用同步方式触发日志，这时会调用这个同步方法。我们在这里想办法
        复用异步逻辑，实在不行就退化成"只清洗 standard_logging_object"。
        """
        import asyncio    # 异步运行时库，用来判断/驱动事件循环

        try:
            loop = asyncio.get_event_loop()   # 拿到当前的"事件循环"
            if loop.is_running():
                # 事件循环正在运行：这里不能再用 run_until_complete（会报错），
                # 改走纯同步的等价清洗 _sync_scrub。
                return self._sync_scrub(kwargs, result)
            # 事件循环没在运行：可以安全地"跑一遍"异步钩子并等它完成
            return loop.run_until_complete(
                self.async_logging_hook(kwargs, result, call_type)
            )
        except Exception:
            # 拿不到/无法驱动事件循环（如 Python≥3.12 无 running loop、
            # 子线程未建 loop）。
            # 不能就此静默放弃清洗：记录 warning 后退回纯同步清洗 _sync_scrub 兜底，
            # 至少保证 standard_logging_object 被处理；连兜底都失败才原样返回。
            logger.warning(
                "logging_hook asyncio fallback failed; trying sync scrub",
                exc_info=True,
            )
            try:
                return self._sync_scrub(kwargs, result)
            except Exception:
                return kwargs, result

    def _sync_scrub(self, kwargs, result):
        """
        纯同步的清洗（_sync 版本）。

        它只做"最核心、最必要"的一件事：就地清洗 standard_logging_object
        里的目标字段（因为这才是真正决定数据库存什么的地方）。
        不去碰 result 副本，避免在同步上下文里做复杂操作。
        清洗实现与主路径共用 _scrub_slo_response，保证语义一致。
        """
        try:
            matched = _match_rule(_extract_api_base(kwargs))  # 找 api_base 并匹配规则
            if not matched:                  # 没命中：原样返回
                return kwargs, result
            prefix, token_lists = matched
            replaced, capped, saved = _scrub_slo_response(
                kwargs.get("standard_logging_object"), token_lists
            )
            if replaced == 0:
                logger.warning(
                    "sync scrub matched but replaced nothing "
                    "(rule prefix=%s, paths=%s); check response shape",
                    prefix, [p for p, _ in token_lists],
                )
            else:
                logger.info(
                    "sync scrubbed response (rule prefix=%s): "
                    "%d string leaf/leaves replaced, ~%d chars saved, "
                    "%d container(s) depth-capped",
                    prefix, replaced, saved, capped,
                )
            return kwargs, result
        except Exception:
            # 出错不影响主流程，但必须留下痕迹：兜底路径一旦失效，
            # 整行日志就会带着敏感数据原样入库，静默等于漏清洗。
            logger.warning("sync scrub failed; writing as-is", exc_info=True)
            return kwargs, result


# 创建一个全局唯一的实例。LiteLLM 通过配置文件里写的
# "custom_logger.base64_scrub_logger_instance" 这个名字找到它并注册为回调。
base64_scrub_logger_instance = Base64ScrubLogger()
