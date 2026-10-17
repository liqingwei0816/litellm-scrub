"""
custom_logger 清洗语义测试套件（pytest + hypothesis + jsonschema）。

运行方式：在钉死的 litellm 镜像内跑，这样 `litellm` 是真实导入，
不需要任何 stub。依赖（pytest/hypothesis/jsonschema/coverage）以
linux cp313 wheel 解包后挂载进容器：

    docker run --rm -v <repo>:/work -v <deps>:/deps -w /work \
        -e PYTHONPATH=/deps -e SCRUB_RULES_PATH=/work/scrub_rules.yaml \
        --entrypoint /app/.venv/bin/python3 \
        ghcr.io/berriai/litellm:v1.83.14-stable -m pytest tests/test_custom_logger.py -q

覆盖六组东西：
1. 结构守恒（方案 D 契约的核心）：键路径集合 / 值类型 / 每层数组长度
2. 不泄露：原字符串叶子的任何长片段都不出现在清洗后的文本里
3. 幂等 / 深度上限 / 循环引用 / 三条执行路径 / 规则加载的失败与兼容
4. 与旧"删除语义"的差分：逐处差异必须是契约里写明"预期"的那几处
5. 官方 OpenAI 响应形状的 jsonschema 校验 + hypothesis 属性测试
6. 性能 / 递归安全 / 内存门禁（原地替换不得整树拷贝），以及为覆盖率
   门禁补齐的分支用例
"""

import asyncio
import copy
import json
import os
import sys
import time
import tracemalloc

from hypothesis import given, settings, strategies as st
import jsonschema
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import custom_logger as cl


PH = cl.SCRUB_PLACEHOLDER


# --------------------------------------------------------------------
# 结构摘要工具：与实现完全无关的独立实现，用于"结构守恒"断言
# --------------------------------------------------------------------
def shape_of(value):
    """
    递归摘要一个 JSON 值的"形状"：
      dict -> {键: 子形状}，list -> [子形状...]，str -> "str"，其它 -> 类型名
    注意字符串叶子只记类型不记值：契约允许字符串的值被换掉，
    但不允许它的类型、所在的键、所在数组的位置发生变化。
    """
    if isinstance(value, dict):
        return {k: shape_of(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shape_of(v) for v in value]
    if isinstance(value, str):
        return "str"
    return type(value).__name__


def key_paths(value, prefix=()):
    """返回所有嵌套键路径的集合（含中间层），用于断言"一个键都没丢"。"""
    found = set(prefix)
    if isinstance(value, dict):
        for k, v in value.items():
            found |= key_paths(v, (*prefix, k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            found |= key_paths(v, (*prefix, f"[{i}]"))
    return found


def string_leaves(value):
    """收集所有字符串叶子（用于泄露扫描）。"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in string_leaves(v)]
    if isinstance(value, list):
        return [s for v in value for s in string_leaves(v)]
    return []


def get_at(obj, path):
    """按 'choices[*].message.content' 这类路径取第一项的值（仅测试用）。"""
    tokens = cl._tokenize(path)
    node = obj
    for kind, val in tokens:
        # 键与下标都是直接取值；通配符固定取第 0 个元素
        node = node[val] if kind in (cl._KEY, cl._IDX) else node[0]
    return node


# --------------------------------------------------------------------
# 真实形状 fixtures（按官方 OpenAI API 响应结构构造）
# --------------------------------------------------------------------
BIG_B64 = "data:image/png;base64," + ("QmFzZTY0RmFrZUJsb2I=" * 400)
AUDIO_B64 = "AAA" * 2000


@pytest.fixture
def chat_response():
    return {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": BIG_B64,
                    "audio": None,
                    "reasoning_content": "thinking...",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 9, "completion_tokens": 12, "total_tokens": 21},
    }


@pytest.fixture
def image_response():
    return {
        "created": 1700000000,
        "data": [
            {"b64_json": AUDIO_B64, "revised_prompt": "a cat", "url": None},
            {"b64_json": "Zm9v", "revised_prompt": None, "url": "https://x/y.png"},
        ],
    }


@pytest.fixture
def container_response():
    """路径终点是"容器"的用例：choices[*].message.images 是数组。"""
    return {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "keep me",
                    "images": [
                        {"url": "SECRET1", "detail": "high", "w": 10},
                        {"url": "SECRET2", "detail": "low", "tags": ["t1", "t2"]},
                        {},
                        "bare-string-element",
                    ],
                },
            }
        ]
    }


# --------------------------------------------------------------------
# 1. 结构守恒：方案 D 契约的正身
# --------------------------------------------------------------------
def test_leaf_string_target_preserves_keys_and_type(chat_response):
    before = copy.deepcopy(chat_response)
    n, c, _s = cl._scrub_path(chat_response, cl._tokenize("choices[*].message.content"))

    assert n == 1 and c == 0
    # 键路径集合、形状完全一致
    assert key_paths(chat_response) == key_paths(before)
    assert shape_of(chat_response) == shape_of(before)
    # 值被换成占位符，且仍是字符串
    content = chat_response["choices"][0]["message"]["content"]
    assert content == PH
    # 同层其它字段一个没动
    msg = chat_response["choices"][0]["message"]
    assert msg["role"] == "assistant"
    assert msg["audio"] is None
    assert msg["reasoning_content"] == "thinking..."
    # 数组长度守恒
    assert len(chat_response["choices"]) == len(before["choices"]) == 1
    # usage 里非目标字段不变
    assert chat_response["usage"] == before["usage"]


def test_non_string_leaves_are_not_replaced(container_response):
    """int / bool / None / 空容器原样保留：赋字符串会破坏类型守恒。"""
    cl._scrub_path(container_response, cl._tokenize("choices[*].message.images"))
    images = container_response["choices"][0]["message"]["images"]

    assert len(images) == 4                    # 元素个数守恒
    assert images[0]["w"] == 10                # int 保留
    assert images[0]["detail"] == PH           # str 替换
    assert images[1]["tags"] == [PH, PH]       # 嵌套数组里的 str 替换，长度守恒
    assert images[2] == {}                     # 空对象原样保留
    assert images[3] == PH                     # 数组里的裸字符串元素也替换
    assert shape_of(images) == [
        {"url": "str", "detail": "str", "w": "int"},
        {"url": "str", "detail": "str", "tags": ["str", "str"]},
        {},
        "str",
    ]


def test_container_target_replaces_all_nested_strings(container_response):
    before = copy.deepcopy(container_response)
    n, c, _s = cl._scrub_path(
        container_response, cl._tokenize("choices[*].message.images")
    )
    # 被替换的字符串叶子：url*2 + detail*2 + tags*2 + 裸元素 = 7
    assert n == 7 and c == 0
    assert key_paths(container_response) == key_paths(before)
    assert shape_of(container_response) == shape_of(before)
    # 目标之外的字符串（message.content）不受影响
    assert container_response["choices"][0]["message"]["content"] == "keep me"


def test_terminal_wildcard_keeps_array_length(image_response):
    """尾部 [*]：旧语义是 clear() 清空，新语义逐项清洗、长度守恒。"""
    before_len = len(image_response["data"])
    n, _, _ = cl._scrub_path(image_response, cl._tokenize("data[*]"))
    assert len(image_response["data"]) == before_len == 2
    assert all(isinstance(item, dict) and item["b64_json"] == PH
               for item in image_response["data"])
    assert n == 4  # 两个 b64_json + 两个 revised_prompt（None 不算）


def test_indexed_path_and_negative_index(chat_response):
    cl._scrub_path(chat_response, cl._tokenize("choices[-1].message.content"))
    assert chat_response["choices"][0]["message"]["content"] == PH

    other = copy.deepcopy(chat_response)
    cl._scrub_path(other, cl._tokenize("choices[1].message.content"))  # 越界
    assert other == chat_response  # 越界应完全无副作用


def test_idempotent_no_nesting(chat_response):
    """同一份数据反复清洗：值稳定、计数不再增长（幂等）。"""
    tokens = cl._tokenize("choices[*].message.content")
    first = cl._scrub_path(chat_response, tokens)
    second = cl._scrub_path(chat_response, tokens)
    assert first[0] == 1 and second[0] == 0
    assert chat_response["choices"][0]["message"]["content"] == PH
    # 连跑五次也还是同一个形状同一个值
    for _ in range(5):
        cl._scrub_path(chat_response, tokens)
    assert chat_response["choices"][0]["message"]["content"] == PH


def test_idempotent_container_target_counts_do_not_inflate():
    """
    终点是容器时走的是 _scrub_subtree，它自己也有"已是占位符就不计数"的判断；
    上面的用例终点是字符串叶子（走 _scrub_value），挡不到这条分支。
    第二趟必须报 (0, 0, 0)：计数虚高会让"这次到底改了多少"的日志失去意义。
    """
    resp = {"payload": {"a": "S" * 500, "b": ["x" * 300], "c": {"d": "y" * 300}}}
    shape_before = shape_of(resp["payload"])
    tokens = cl._tokenize("payload")

    replaced, capped, saved = cl._scrub_path(resp, tokens)
    assert (replaced, capped) == (3, 0)
    assert saved == (500 - len(PH)) + (300 - len(PH)) + (300 - len(PH))
    assert cl._scrub_path(resp, tokens) == (0, 0, 0)
    for _ in range(5):
        assert cl._scrub_path(resp, tokens) == (0, 0, 0)
    # 既不套娃，也不改形状
    assert string_leaves(resp["payload"]) == [PH, PH, PH]
    assert shape_of(resp["payload"]) == shape_before


def test_round_trip_json_stays_parseable(chat_response, image_response):
    for obj, path in ((chat_response, "choices[*].message.content"),
                      (image_response, "data[*].b64_json")):
        cl._scrub_path(obj, cl._tokenize(path))
        text = json.dumps(obj, ensure_ascii=False)
        assert json.loads(text) == obj


# --------------------------------------------------------------------
# 2. 泄露：原值必须彻底消失
# --------------------------------------------------------------------
@pytest.mark.parametrize("window", [32, 64])
def test_no_long_substring_of_original_survives(chat_response, image_response, window):
    targets = [
        (chat_response, "choices[*].message.content"),
        (image_response, "data[*].b64_json"),
    ]
    originals = []
    for obj, path in targets:
        originals.append(get_at(obj, path))
        cl._scrub_path(obj, cl._tokenize(path))

    dumped = json.dumps(chat_response) + json.dumps(image_response)
    for original in originals:
        assert original not in dumped
        # 抽样滑动窗口：任何 window 长度的连续片段都不许出现
        for i in range(0, len(original) - window, max(1, window)):
            assert original[i:i + window] not in dumped


def test_placeholder_carries_no_metadata():
    """占位符不得含原长度/类型/hash —— 精确长度就是内容指纹。"""
    assert PH == "<scrubbed>"
    obj = {"a": "x" * 5000}
    cl._scrub_path(obj, cl._tokenize("a"))
    assert obj["a"] == PH and len(obj["a"]) == len(PH)


# --------------------------------------------------------------------
# 3. 属性测试：守恒是"性质"，不是几个例子
# --------------------------------------------------------------------
json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-10_000, max_value=10_000),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=24),
)
json_values = st.recursive(
    json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(min_size=1, max_size=6), children, max_size=4),
    ),
    max_leaves=25,
)


@settings(max_examples=200, deadline=None)
@given(payload=json_values)
def test_property_structure_preserved_for_container_target(payload):
    """任意 JSON 值塞进目标位置，清洗后形状必须与原始完全一致。"""
    response = {"choices": [{"index": 0, "message": {"role": "assistant",
                                                     "images": payload}}]}
    before = copy.deepcopy(response)
    original_leaves = string_leaves(payload)
    cl._scrub_path(response, cl._tokenize("choices[*].message.images"))

    assert shape_of(response) == shape_of(before)
    assert key_paths(response) == key_paths(before)
    assert json.loads(json.dumps(response)) == response
    # 幂等：再洗一次完全不变
    twice = copy.deepcopy(response)
    cl._scrub_path(twice, cl._tokenize("choices[*].message.images"))
    assert twice == response
    # 体积上界（契约第 0 节要求的不等式）：
    #   净增量 ≤ Σ max(0, len(占位符) - len(原串))
    # 只有"原串比占位符短"时才会变大，单个叶子最多变大 len(占位符) 个字符
    # （原串为空串时取到等号）。
    allowed_growth = sum(max(0, len(PH) - len(s)) for s in original_leaves)
    actual_delta = (len(json.dumps(response, ensure_ascii=False))
                    - len(json.dumps(before, ensure_ascii=False)))
    assert actual_delta <= allowed_growth
    # 注意：不承诺"总体积必然下降"。子树里一个长串省下的量，可能被许多
    # 短串的膨胀抵销。"必然下降"只在载荷远长于占位符的真实场景成立，
    # 由 test_real_payload_shrinks_dramatically 用真实尺寸载荷单独证明。


def test_worst_case_size_bound_all_short_strings():
    """最坏情况显式记账：目标子树全是空串时，膨胀量正好取到上界等号。"""
    n = 50
    response = {"data": [{"v": ""} for _ in range(n)]}
    before = len(json.dumps(response))
    cl._scrub_path(response, cl._tokenize("data[*]"))
    delta = len(json.dumps(response)) - before
    assert delta == n * len(PH)              # 每个空串变成 len(PH) 个字符
    # 上界只与"被替换的叶子数"有关，与被替换掉的原串大小无关
    assert delta / n == len(PH)


def test_real_payload_shrinks_dramatically(chat_response, image_response):
    """真实尺寸用例：base64 载荷被换掉后体积断崖式下降。"""
    for obj, path in ((chat_response, "choices[*].message.content"),
                      (image_response, "data[*].b64_json")):
        before = len(json.dumps(obj, ensure_ascii=False))
        cl._scrub_path(obj, cl._tokenize(path))
        after = len(json.dumps(obj, ensure_ascii=False))
        assert after / before < 0.05, f"{path}: {before} -> {after}"


@settings(max_examples=200, deadline=None)
@given(text=st.text(min_size=1, max_size=500))
def test_property_string_target_always_becomes_placeholder(text):
    response = {"choices": [{"message": {"content": text}}]}
    n, _, _ = cl._scrub_path(response, cl._tokenize("choices[*].message.content"))
    assert response["choices"][0]["message"]["content"] == PH
    # 原本就是占位符时计数为 0，其余为 1
    assert n == (0 if text == PH else 1)


@settings(max_examples=100, deadline=None)
@given(payload=json_values, extra=st.dictionaries(st.text(min_size=1, max_size=6),
                                                  json_scalars, max_size=4))
def test_property_outside_target_untouched(payload, extra):
    response = {"choices": [{"message": {"images": payload}}],
                "usage": extra, "id": "keep-me"}
    before = copy.deepcopy(response)
    cl._scrub_path(response, cl._tokenize("choices[*].message.images"))
    assert response["usage"] == before["usage"]
    assert response["id"] == "keep-me"


# --------------------------------------------------------------------
# 4. jsonschema：机器判定"下游还能按官方结构解析"
# --------------------------------------------------------------------
CHAT_SCHEMA = {
    "type": "object",
    "required": ["id", "object", "created", "model", "choices", "usage"],
    "properties": {
        "id": {"type": "string"},
        "object": {"const": "chat.completion"},
        "created": {"type": "integer"},
        "model": {"type": "string"},
        "choices": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["index", "message", "finish_reason"],
                "properties": {
                    "index": {"type": "integer"},
                    "message": {
                        "type": "object",
                        "required": ["role", "content"],
                        "properties": {
                            "role": {"const": "assistant"},
                            "content": {"type": ["string", "null"]},
                            "audio": {"type": ["object", "null"]},
                            "reasoning_content": {"type": ["string", "null"]},
                        },
                    },
                    "finish_reason": {"type": "string"},
                },
            },
        },
        "usage": {
            "type": "object",
            "required": ["prompt_tokens", "completion_tokens", "total_tokens"],
            "properties": {k: {"type": "integer"} for k in
                           ("prompt_tokens", "completion_tokens", "total_tokens")},
        },
    },
}

IMAGE_SCHEMA = {
    "type": "object",
    "required": ["created", "data"],
    "properties": {
        "created": {"type": "integer"},
        "data": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "b64_json": {"type": ["string", "null"]},
                    "revised_prompt": {"type": ["string", "null"]},
                    "url": {"type": ["string", "null"]},
                },
            },
        },
    },
}


def test_scrubbed_output_validates_against_official_schemas(chat_response,
                                                           image_response):
    cl._scrub_path(chat_response, cl._tokenize("choices[*].message.content"))
    cl._scrub_path(image_response, cl._tokenize("data[*].b64_json"))
    jsonschema.validate(chat_response, CHAT_SCHEMA)
    jsonschema.validate(image_response, IMAGE_SCHEMA)


def test_scrubbed_container_output_validates(container_response):
    cl._scrub_path(container_response, cl._tokenize("choices[*].message.images"))
    jsonschema.validate(container_response, {
        "type": "object",
        "properties": {
            "choices": {"type": "array", "minItems": 1, "items": {
                "type": "object",
                "properties": {"message": {"type": "object", "properties": {
                    "images": {"type": "array", "minItems": 4, "maxItems": 4},
                }}},
            }},
        },
    })


# --------------------------------------------------------------------
# 5. 差分：新语义相对旧"删除语义"的差异必须逐处是预期的
# --------------------------------------------------------------------
def legacy_remove_path(obj, tokens):
    """改动前的删除语义（仅作差分基准，不参与任何生产路径）。"""
    if not tokens:
        return
    kind, val = tokens[0]
    last = len(tokens) == 1
    if kind == cl._KEY:
        if not isinstance(obj, dict) or val not in obj:
            return
        if last:
            del obj[val]
        else:
            legacy_remove_path(obj[val], tokens[1:])
    elif kind == cl._IDX:
        if not isinstance(obj, list) or not (-len(obj) <= val < len(obj)):
            return
        if last:
            obj.pop(val)
        else:
            legacy_remove_path(obj[val], tokens[1:])
    elif kind == cl._WILD:
        if not isinstance(obj, list):
            return
        if last:
            obj.clear()
        else:
            for item in obj:
                legacy_remove_path(item, tokens[1:])


@pytest.mark.parametrize(
    "fixture_name,path",
    [("chat_response", "choices[*].message.content"),
     ("image_response", "data[*].b64_json"),
     ("container_response", "choices[*].message.images")],
)
def test_differential_old_vs_new_only_differs_where_contract_says(
    fixture_name, path, request
):
    obj = request.getfixturevalue(fixture_name)
    legacy = copy.deepcopy(obj)
    new = copy.deepcopy(obj)
    tokens = cl._tokenize(path)

    legacy_remove_path(legacy, tokens)
    cl._scrub_path(new, tokens)

    # 差异必须集中在目标键/元素上，其余字节级相同
    assert json.loads(json.dumps(new)) == new
    target_shape_diff = shape_of(legacy) != shape_of(new)
    assert target_shape_diff, "旧语义删键、新语义保键，形状必然不同"
    # 新语义相对原始：形状与键完全一致（这才是本次改动的目的）
    assert shape_of(new) == shape_of(obj)
    assert key_paths(new) == key_paths(obj)
    # 旧语义相对原始：键或长度发生了变化
    assert key_paths(legacy) != key_paths(obj) or shape_of(legacy) != shape_of(obj)


# --------------------------------------------------------------------
# 6. 健壮性矩阵
# --------------------------------------------------------------------
def test_mismatched_path_is_silent_noop(chat_response):
    before = copy.deepcopy(chat_response)
    for bad in ("choices[*].nonexistent", "not_a_list[0]", "choices[9].message.content",
                "choices[*].message.content.content"):
        assert cl._scrub_path(before, cl._tokenize(bad)) == (0, 0, 0)
    assert before == chat_response


def test_non_dict_container_is_tolerated():
    for obj in ("plain string", 42, None, [1, 2, 3]):
        assert cl._scrub_path(obj, cl._tokenize("a.b")) == (0, 0, 0)


def test_empty_containers_kept():
    obj = {"data": [], "meta": {}, "x": ""}
    cl._scrub_path(obj, cl._tokenize("data"))
    cl._scrub_path(obj, cl._tokenize("meta"))
    assert obj["data"] == [] and obj["meta"] == {}
    # 空字符串是字符串叶子：按契约也会被替换
    cl._scrub_path(obj, cl._tokenize("x"))
    assert obj["x"] == PH


def test_deeply_nested_does_not_recursion_error():
    """深度 300 的子树：显式栈 + 限深，绝不 RecursionError。"""
    node = {"leaf": "SECRET"}
    root = cur = node
    for _ in range(300):
        cur["next"] = {"leaf2": "SECRET2"}
        cur = cur["next"]

    n, c, _ = cl._scrub_path(root, cl._tokenize("leaf"))
    assert n == 1 and c == 0
    # 清洗整棵超深子树：靠 MAX_SCRUB_DEPTH 截停并回报 capped
    n, c, _ = cl._scrub_path({"t": node}, cl._tokenize("t"))
    assert c > 0, "超过 MAX_SCRUB_DEPTH 的容器必须被计数回报"


def test_self_referential_structure_terminates():
    """循环引用：靠深度上限终止，不挂死、不抛栈错误。"""
    d = {"a": "SECRET"}
    d["self"] = d
    new_value, n, c, _saved = cl._scrub_value(d)
    assert new_value is d and n >= 1 and c > 0


def test_depth_cap_reports_and_stops():
    deep = {"leaf": "SECRET"}
    cur = deep
    for _ in range(cl.MAX_SCRUB_DEPTH + 10):
        cur["next"] = {"leaf": "SECRET"}
        cur = cur["next"]
    new_value, _n, c, _saved = cl._scrub_value(deep)
    assert new_value is deep
    assert c > 0                       # 有容器被限深截停
    assert "SECRET" in string_leaves(deep)  # 截停之下的字符串确实没被换（fail-open）


# --------------------------------------------------------------------
# 7. 规则加载：兼容、非法、缺失
# --------------------------------------------------------------------
def write_rules(tmp_path, text):
    # 唯一文件名：同一 tmp_path 下重复写 rules.yaml 会让后一次覆盖前一次
    write_rules.counter += 1
    p = tmp_path / f"rules-{write_rules.counter}.yaml"
    p.write_text(text, encoding="utf-8")
    return str(p)


write_rules.counter = 0


def test_loader_accepts_scrub_paths_and_deprecated_remove_paths(tmp_path, caplog):
    new_key = write_rules(tmp_path, """
rules:
  - api_base_prefix: "https://a.example.com"
    scrub_paths: ["choices[*].message.content"]
""")
    old_key = write_rules(tmp_path, """
rules:
  - api_base_prefix: "https://b.example.com"
    remove_paths: ["data[*].b64_json"]
""")
    with caplog.at_level("INFO"):
        compiled_new = cl._load_rules(new_key)
        compiled_old = cl._load_rules(old_key)

    assert compiled_new and compiled_new[0][0] == "https://a.example.com"
    # 旧键仍然生效（否则既有部署会静默失去清洗）
    assert compiled_old and compiled_old[0][0] == "https://b.example.com"
    assert any("deprecated" in r.message for r in caplog.records)


def test_loader_fails_loud_on_invalid_path(tmp_path, caplog):
    p = write_rules(tmp_path, """
rules:
  - api_base_prefix: "https://a.example.com"
    scrub_paths: ["choices[abc].message", "ok.path"]
""")
    with caplog.at_level("INFO"):
        compiled = cl._load_rules(p)
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("invalid scrub_path" in r.message for r in errors)
    # 非法路径被丢掉，合法路径仍保留（不因一条写错而整条规则失效）
    assert [t for _, toks in compiled for t, _ in toks] and len(compiled[0][1]) == 1


def test_loader_rejects_too_many_tokens(tmp_path, caplog):
    long_path = ".".join(f"k{i}" for i in range(cl.MAX_PATH_TOKENS + 20))
    p = write_rules(tmp_path, f"""
rules:
  - api_base_prefix: "https://a.example.com"
    scrub_paths: ["{long_path}"]
""")
    with caplog.at_level("INFO"):
        compiled = cl._load_rules(p)
    assert any("too many tokens" in r.message for r in caplog.records)
    assert compiled == []


def test_loader_missing_file_is_not_fatal(tmp_path, caplog):
    with caplog.at_level("INFO"):
        compiled = cl._load_rules(str(tmp_path / "nope.yaml"))
    assert compiled == []
    assert any("not found" in r.message for r in caplog.records)


def test_loader_empty_and_broken_yaml_are_not_fatal(tmp_path):
    assert cl._load_rules(write_rules(tmp_path, "")) == []
    assert cl._load_rules(write_rules(tmp_path, "rules: [")) == []
    assert cl._load_rules(write_rules(tmp_path, "rules: []")) == []


def test_repo_rules_file_loads_with_scrub_paths():
    """仓库里那份真实规则文件必须能加载出两条 scrub_paths 规则。"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scrub_rules.yaml")
    compiled = cl._load_rules(path)
    assert len(compiled) == 2
    assert all(toks for _, toks in compiled)


def test_loader_reports_duplicate_prefix(tmp_path, caplog):
    """
    同一前缀写了两条规则 = 第二条的 scrub_paths 永远不生效（_match_rule 命中即返回）。

    这属于静默漏清洗，和"路径写错"同级，必须在启动日志里报 ERROR。
    """
    p = write_rules(tmp_path, """
rules:
  - api_base_prefix: "https://dup.example.com"
    scrub_paths: ["choices[*].message.content"]
  - api_base_prefix: "HTTPS://dup.example.com/"
    scrub_paths: ["data[*].b64_json"]
""")
    with caplog.at_level("INFO"):
        compiled = cl._load_rules(p)
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("duplicate api_base_prefix" in m for m in errors)
    # 两条都还在列表里（归一化后前缀相同），只是第二条永不命中
    assert len(compiled) == 2
    assert compiled[0][0] == compiled[1][0] == "https://dup.example.com"


def test_first_of_duplicate_prefixes_wins(monkeypatch):
    """重复前缀的实际后果：只有先出现的那条规则被应用。"""
    rules = [
        ("https://dup.example.com", [("a", cl._tokenize("a"))]),
        ("https://dup.example.com", [("b", cl._tokenize("b"))]),
    ]
    rules.sort(key=lambda r: len(r[0]), reverse=True)
    monkeypatch.setattr(cl, "_RULES", rules)
    prefix, token_lists = cl._match_rule("https://dup.example.com/v1")
    assert prefix == "https://dup.example.com"
    assert [p for p, _ in token_lists] == ["a"]


# --------------------------------------------------------------------
# 8. api_base 匹配与提取（含既有安全边界）
# --------------------------------------------------------------------
@pytest.fixture
def patched_rules():
    original = cl._RULES
    cl._RULES = cl._load_rules(os.devnull) or []
    cl._RULES = [
        ("https://provider-a.example.com", [("choices[*].message.content",
                                             cl._tokenize("choices[*].message.content"))]),
        ("https://provider-b.example.com/v1", [("data[*].b64_json",
                                                cl._tokenize("data[*].b64_json"))]),
    ]
    cl._RULES.sort(key=lambda r: len(r[0]), reverse=True)
    yield
    cl._RULES = original


@pytest.mark.parametrize(
    "api_base,expected",
    [("https://provider-a.example.com/v1", "https://provider-a.example.com"),
     ("HTTPS://Provider-A.example.com/V1/", "https://provider-a.example.com"),
     ("https://provider-b.example.com/v1", "https://provider-b.example.com/v1"),
     ("https://provider-x.example.com/v1", None),
     ("https://provider-a.example.com.evil.com/v1", None),   # 异域名不得误命中
     ("https://provider-a.example.comX/v1", None),
     ("", None), (None, None), (123, None)],
)
def test_match_rule_boundaries(patched_rules, api_base, expected):
    matched = cl._match_rule(api_base)
    assert (matched[0] if matched else None) == expected


def test_extract_api_base_priority_order():
    kb = {"litellm_params": {"metadata": {"api_base": "https://m.example.com"}}}
    assert cl._extract_api_base(kb) == "https://m.example.com"
    kb = {"standard_logging_object": {"model_parameters":
                                      {"api_base": "https://mp.example.com"}}}
    assert cl._extract_api_base(kb) == "https://mp.example.com"
    assert cl._extract_api_base({}) == ""
    # metadata 不是 dict 时不得抛错
    assert cl._extract_api_base({"litellm_params": {"metadata": "oops"}}) == ""


@pytest.mark.parametrize("bad", [None, "oops", 42, [], object()])
def test_extract_api_base_tolerates_non_dict_layers(bad):
    """
    kwargs 的每一层都可能不是 dict：任何一层都不得让提取抛异常。

    抛异常的代价是"整次清洗被最外层兜底跳过、敏感数据原样入库"，比"这次找不到
    api_base"严重得多，所以每一层都过 _as_dict 退化成空字典。
    """
    assert cl._extract_api_base({
        "litellm_params": bad,
        "standard_logging_object": bad,
        "optional_params": bad,
    }) == ""
    assert cl._extract_api_base({
        "litellm_params": {"metadata": bad, "api_base": "https://top.example.com"},
        "standard_logging_object": {"metadata": bad, "model_parameters": bad},
    }) == "https://top.example.com"


def test_extract_api_base_falls_through_empty_strings():
    """高优先级的键存在但值为空串：必须继续往下找，而不是返回空。"""
    kb = {
        "litellm_params": {"metadata": {"api_base": ""}, "api_base": None},
        "standard_logging_object": {"api_base": "https://slo.example.com"},
    }
    assert cl._extract_api_base(kb) == "https://slo.example.com"
    kb = {
        "litellm_params": {"metadata": {"api_base": None}, "api_base": ""},
        "standard_logging_object": {"metadata": {"api_base": "https://slo-meta.example.com"}},
    }
    assert cl._extract_api_base(kb) == "https://slo-meta.example.com"
    kb = {"optional_params": {"api_base": "https://opt.example.com"}}
    assert cl._extract_api_base(kb) == "https://opt.example.com"


# --------------------------------------------------------------------
# 9. 三条执行路径 + 客户端零影响
# --------------------------------------------------------------------
def make_kwargs(api_base, response):
    return {
        "litellm_call_id": "call-1",
        "litellm_params": {"metadata": {"api_base": api_base}},
        "standard_logging_object": {"response": response},
    }


def test_async_hook_scrubs_and_leaves_result_object_untouched(patched_rules,
                                                              chat_response):
    logger_obj = cl.Base64ScrubLogger()
    # result 是与 slo["response"] 独立的对象（LiteLLM 的真实情形）
    result = {"choices": [{"message": {"content": BIG_B64}}], "id": "x"}
    result_before = copy.deepcopy(result)
    kwargs = make_kwargs("https://provider-a.example.com/v1", chat_response)

    out_kwargs, out_result = asyncio.run(
        logger_obj.async_logging_hook(kwargs, result, "acompletion")
    )
    assert out_kwargs is kwargs
    # 客户端对象的类型与内容都不得变化（旧注释里的 tool_calls 回归防护）
    assert out_result is result and out_result == result_before
    assert out_kwargs["standard_logging_object"]["response"]["choices"][0][
        "message"]["content"] == PH


def test_async_hook_noop_when_rule_misses(patched_rules, chat_response):
    before = copy.deepcopy(chat_response)
    kwargs = make_kwargs("https://provider-x.example.com/v1", chat_response)
    out_kwargs, _ = asyncio.run(
        cl.Base64ScrubLogger().async_logging_hook(kwargs, None, "acompletion")
    )
    assert out_kwargs["standard_logging_object"]["response"] == before


def test_async_hook_warns_when_matched_but_nothing_replaced(patched_rules, caplog):
    kwargs = make_kwargs("https://provider-a.example.com/v1",
                         {"choices": [{"message": {"role": "assistant"}}]})
    with caplog.at_level("INFO"):
        asyncio.run(cl.Base64ScrubLogger().async_logging_hook(kwargs, None, "x"))
    assert any("matched but replaced nothing" in r.message for r in caplog.records)


def test_async_hook_swallows_exceptions_and_keeps_pipeline(patched_rules, caplog):
    """response 不是 dict / kwargs 畸形：必须原样透传且不抛异常。"""
    weird = [{"cycle": None}]
    weird[0]["cycle"] = weird
    kwargs = make_kwargs("https://provider-a.example.com/v1", weird)
    out_kwargs, out_result = asyncio.run(
        cl.Base64ScrubLogger().async_logging_hook(kwargs, None, "x")
    )
    assert out_kwargs is kwargs and out_result is None

    for bad in ({"standard_logging_object": None}, {}, None):
        if bad is None:
            continue
        # 不抛异常即为通过；LiteLLM 的容错承诺优先于清洗
        asyncio.run(cl.Base64ScrubLogger().async_logging_hook(
            make_kwargs("https://provider-a.example.com/v1", bad)
            if isinstance(bad, dict) and "standard_logging_object" in bad else bad,
            None, "x"))


def test_logging_hook_scrubs_like_async_path(patched_rules, chat_response):
    kwargs = make_kwargs("https://provider-a.example.com/v1", chat_response)
    out_kwargs, out_result = cl.Base64ScrubLogger().logging_hook(kwargs, "R", "x")
    assert out_kwargs is kwargs and out_result == "R"
    assert kwargs["standard_logging_object"]["response"]["choices"][0][
        "message"]["content"] == PH


def test_both_hook_paths_emit_identical_logs(patched_rules, caplog):
    """
    C13 的硬口径：两条路径的清洗结果与日志必须逐条一致。

    曾经 sync 那份日志逻辑漏了"超出限深"的 WARNING —— 同一个超深载荷走主路径
    会告警、走兜底路径就静默。这里用同一个载荷分别喂两个钩子，断言除 hook= 标签
    外日志完全相同。
    """
    def build():
        deep = {"leaf": "SECRET"}
        cur = deep
        for _ in range(cl.MAX_SCRUB_DEPTH + 3):
            cur["next"] = {"leaf": "SECRET"}
            cur = cur["next"]
        return {"choices": [{"message": {"content": deep}}]}

    kwargs_async = make_kwargs("https://provider-a.example.com/v1", build())
    kwargs_sync = make_kwargs("https://provider-a.example.com/v1", build())

    with caplog.at_level("INFO"):
        asyncio.run(cl.Base64ScrubLogger().async_logging_hook(kwargs_async, None, "x"))
    async_msgs = [r.getMessage() for r in caplog.records]
    caplog.clear()
    with caplog.at_level("INFO"):
        cl.Base64ScrubLogger().logging_hook(kwargs_sync, None, "x")
    sync_msgs = [r.getMessage() for r in caplog.records]

    # 清洗结果逐字节一致
    assert (kwargs_async["standard_logging_object"]["response"]
            == kwargs_sync["standard_logging_object"]["response"])
    # 日志一致（只允许 hook= 标签不同）
    def normalize(msgs):
        return sorted(m.replace("hook=async", "hook=X").replace("hook=sync", "hook=X")
                      for m in msgs)
    assert normalize(async_msgs) == normalize(sync_msgs)
    # 兜底路径同样必须报"清洗没覆盖全"
    assert any("MAX_SCRUB_DEPTH" in m for m in sync_msgs)


def test_logging_hook_from_running_loop(patched_rules, chat_response):
    """
    从正在运行的事件循环里调用同步钩子：照样清洗，且不得去驱动循环。

    LiteLLM 的同步调用点（litellm_logging.py:2104）可能在异步上下文里触发，
    此时任何 run_until_complete 都会抛 RuntimeError。清洗全程没有 await，
    所以这里天然安全 —— 这条测试守着它继续安全。
    """
    kwargs = make_kwargs("https://provider-a.example.com/v1", chat_response)
    logger_obj = cl.Base64ScrubLogger()

    async def inside_loop():
        return logger_obj.logging_hook(kwargs, None, "completion")

    asyncio.run(inside_loop())
    assert kwargs["standard_logging_object"]["response"]["choices"][0][
        "message"]["content"] == PH


def test_module_exposes_instance_for_config_callback():
    assert isinstance(cl.base64_scrub_logger_instance, cl.Base64ScrubLogger)


# --------------------------------------------------------------------
# 10. 分支补全：把改动函数的每条分支都跑到（覆盖率门禁用）
# --------------------------------------------------------------------
def test_tokenize_edge_cases():
    # 无名字只有下标：'[0]' 作为第一段合法
    assert cl._tokenize("[0].a") == [(cl._IDX, 0), (cl._KEY, "a")]
    assert cl._tokenize("[*].a") == [(cl._WILD, None), (cl._KEY, "a")]
    # 非法写法
    assert cl._tokenize("a[abc]") is None
    assert cl._tokenize("[[[") is None
    assert cl._tokenize("") is None            # 空路径
    assert cl._tokenize("choices[0].message") == [
        (cl._KEY, "choices"), (cl._IDX, 0), (cl._KEY, "message")]


def test_scrub_path_empty_and_unknown_tokens():
    obj = {"a": "SECRET"}
    assert cl._scrub_path(obj, []) == (0, 0, 0)
    assert cl._scrub_path(obj, [("bogus", "x")]) == (0, 0, 0)   # 未知记号类型
    assert obj == {"a": "SECRET"}                               # 完全无副作用


def test_scrub_path_terminal_index():
    """终点是数组下标（而非键）：这一支以前从未被执行过。"""
    obj = {"data": ["SECRET0", {"x": "SECRET1"}, 7, None]}
    n, c, s = cl._scrub_path(obj, cl._tokenize("data[0]"))
    assert (n, c) == (1, 0) and obj["data"][0] == PH
    assert len(obj["data"]) == 4                               # 长度守恒
    # 终点是容器下标：整个元素内部被清洗，元素本身还是 dict
    n, c, s = cl._scrub_path(obj, cl._tokenize("data[1]"))
    assert n == 1 and obj["data"][1] == {"x": PH}
    # 非字符串叶子下标：数字与 null 原样保留，替换数为 0
    n, c, s = cl._scrub_path(obj, cl._tokenize("data[2]"))
    assert (n, c, s) == (0, 0, 0) and obj["data"][2] == 7
    n, c, s = cl._scrub_path(obj, cl._tokenize("data[3]"))
    assert (n, c, s) == (0, 0, 0) and obj["data"][3] is None


def test_scrub_path_wildcard_on_non_list_is_noop():
    obj = {"a": {"b": "SECRET"}}
    assert cl._scrub_path(obj, cl._tokenize("a[*]")) == (0, 0, 0)
    assert obj == {"a": {"b": "SECRET"}}


def test_loader_skips_rules_missing_fields(tmp_path, caplog):
    p = write_rules(tmp_path, """
rules:
  - api_base_prefix: "https://a.example.com"
  - scrub_paths: ["data[*].b64_json"]
  - api_base_prefix: ""
    scrub_paths: ["x"]
""")
    with caplog.at_level("INFO"):
        compiled = cl._load_rules(p)
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) >= 2
    assert compiled == []


def test_async_hook_warns_when_depth_capped(patched_rules, caplog):
    """载荷深过 MAX_SCRUB_DEPTH：必须发 warning 并承认没清洗完全。"""
    deep = {"leaf": "SECRET"}
    cur = deep
    for _ in range(cl.MAX_SCRUB_DEPTH + 5):
        cur["next"] = {"leaf": "SECRET"}
        cur = cur["next"]
    response = {"choices": [{"message": {"content": deep}}]}
    kwargs = make_kwargs("https://provider-a.example.com/v1", response)
    with caplog.at_level("INFO"):
        asyncio.run(cl.Base64ScrubLogger().async_logging_hook(kwargs, None, "x"))
    assert any("depth" in r.message.lower() or "MAX_SCRUB_DEPTH" in r.message
               for r in caplog.records)
    # 顶层字符串叶子确实被换掉了
    assert response["choices"][0]["message"]["content"]["leaf"] == PH


def test_async_hook_never_raises_on_malformed_kwargs(patched_rules, caplog):
    """kwargs 连 dict 都不是：必须被最外层 except 兜住并原样返回。"""
    for bad_kwargs in ([], "not-a-dict", 42):
        with caplog.at_level("INFO"):
            out_kwargs, out_result = asyncio.run(
                cl.Base64ScrubLogger().async_logging_hook(bad_kwargs, "R", "x")
            )
        assert out_kwargs is bad_kwargs and out_result == "R"
    assert any("async_logging_hook failed" in r.message for r in caplog.records)


def test_logging_hook_never_touches_asyncio(patched_rules, chat_response, monkeypatch):
    """
    同步钩子不得依赖 asyncio：取循环的几个 API 全部炸掉也必须照常清洗。

    这条守着"不再用已弃用的 asyncio.get_event_loop()"这个决定 —— 它在 3.12 起
    弃用、3.14 起没有当前循环时直接抛，而清洗本身全程没有 await。
    """
    def boom(*a, **kw):
        raise RuntimeError("no current event loop")

    monkeypatch.setattr(asyncio, "get_event_loop", boom)
    monkeypatch.setattr(asyncio, "get_running_loop", boom)
    monkeypatch.setattr(asyncio, "new_event_loop", boom)
    kwargs = make_kwargs("https://provider-a.example.com/v1", chat_response)
    out_kwargs, out_result = cl.Base64ScrubLogger().logging_hook(kwargs, "R", "completion")
    assert out_kwargs is kwargs and out_result == "R"
    assert kwargs["standard_logging_object"]["response"]["choices"][0][
        "message"]["content"] == PH


def test_logging_hook_miss_is_silent_and_mismatch_warns(patched_rules, caplog):
    # 未命中：静默原样返回，一条日志都不该有
    miss = make_kwargs("https://provider-x.example.com/v1", {"choices": []})
    with caplog.at_level("INFO"):
        out_kwargs, out_result = cl.Base64ScrubLogger().logging_hook(miss, "R", "x")
    assert out_kwargs is miss and out_result == "R"
    assert not caplog.records

    # 命中但路径对不上：必须发 warning（漏清洗不可静默），并标明走的是同步钩子
    caplog.clear()
    empty = make_kwargs("https://provider-a.example.com/v1", {"choices": []})
    with caplog.at_level("INFO"):
        cl.Base64ScrubLogger().logging_hook(empty, "R", "x")
    assert any("matched but replaced nothing" in r.getMessage()
               and "hook=sync" in r.getMessage() for r in caplog.records)


def test_logging_hook_swallows_scrub_failure(patched_rules, caplog, monkeypatch):
    """共用清洗实现抛异常：必须原样返回 + 留 WARNING，绝不把异常抛给 LiteLLM。"""
    def boom(*a, **kw):
        raise RuntimeError("synthetic scrub failure")

    monkeypatch.setattr(cl, "_scrub_and_report", boom)
    kwargs = make_kwargs("https://provider-a.example.com/v1", {"a": "SECRET"})
    with caplog.at_level("INFO"):
        out_kwargs, out_result = cl.Base64ScrubLogger().logging_hook(kwargs, "R", "x")
    assert out_kwargs is kwargs and out_result == "R"
    assert kwargs["standard_logging_object"]["response"]["a"] == "SECRET"   # 原样入库
    assert any("logging_hook failed" in r.getMessage() for r in caplog.records)


def test_int_env_tolerates_bad_values(monkeypatch, caplog):
    """
    整数型环境变量写了非法值：告警并退回默认，绝不让 import 期抛异常。

    这两个常量在模块加载时就要用，int() 抛出来的异常会让整个回调加载失败、
    LiteLLM 连日志都写不了 —— compose 里一个拼写错误不该有这么大后果。
    """
    with caplog.at_level("WARNING"):
        monkeypatch.setenv("SCRUB_TEST_INT", "64")
        assert cl._int_env("SCRUB_TEST_INT", 8) == 64
        monkeypatch.delenv("SCRUB_TEST_INT")
        assert cl._int_env("SCRUB_TEST_INT", 8) == 8          # 未设置：默认值，不告警
        monkeypatch.setenv("SCRUB_TEST_INT", "64x")
        assert cl._int_env("SCRUB_TEST_INT", 8) == 8          # 非法：退回默认
        monkeypatch.setenv("SCRUB_TEST_INT", "")
        assert cl._int_env("SCRUB_TEST_INT", 8) == 8
        monkeypatch.setenv("SCRUB_TEST_INT", "-1")
        assert cl._int_env("SCRUB_TEST_INT", 8) == 8          # 负数无意义：退回默认
        monkeypatch.setenv("SCRUB_TEST_INT", "0")
        assert cl._int_env("SCRUB_TEST_INT", 8) == 0          # 0 是合法值，照用
    msgs = [r.getMessage() for r in caplog.records]
    # 三次非法取值各告警一次；合法取值（64 / 未设置 / 0）一次都不该告警
    assert len(msgs) == 3
    assert sum("is not an integer" in m for m in msgs) == 2
    assert sum("must not be negative" in m for m in msgs) == 1


@pytest.mark.parametrize("slo,expected_warning", [
    # slo 整个缺失：上游此时直接写 "{}"，没有敏感数据，刻意静默
    (None, None),
    # slo 不是 dict（哪天上游换成 pydantic 模型就会走到这里）
    ("not-a-dict", "standard_logging_object is str, not a dict"),
    # slo 是 dict 但没有 response 键
    ({}, None),
    # response 不是 dict：明确告知"原样入库"
    ({"response": ["a"]}, "standard_logging_object['response'] is list, not a dict"),
    ({"response": "text"}, "standard_logging_object['response'] is str, not a dict"),
])
def test_slo_shape_mismatch_is_reported_precisely(slo, expected_warning, caplog):
    """
    形状不符必须"指名道姓"地告警。

    只报"命中规则却零替换，请检查路径"会把排查引向错误方向：真正的原因是
    上游形状变了、isinstance 判定整体失效，一个字段都没换。
    """
    token_lists = [("a.b", cl._tokenize("a.b"))]
    with caplog.at_level("INFO"):
        assert cl._scrub_slo_response(slo, token_lists) == (0, 0, 0)
    msgs = [r.getMessage() for r in caplog.records]
    if expected_warning is None:
        assert not any("not a dict" in m for m in msgs)
    else:
        assert any(expected_warning in m for m in msgs)


def test_scrub_slo_response_applies_every_path():
    """一条规则下的多个路径必须全部执行，且统计是各路径之和。"""
    slo = {"response": {
        "choices": [{"message": {"content": "SECRET-1"}}],
        "data": [{"b64_json": "SECRET-2"}, {"b64_json": "SECRET-3"}],
        "keep": "untouched",
    }}
    token_lists = [
        ("choices[*].message.content", cl._tokenize("choices[*].message.content")),
        ("data[*].b64_json", cl._tokenize("data[*].b64_json")),
    ]
    replaced, capped, saved = cl._scrub_slo_response(slo, token_lists)
    assert (replaced, capped) == (3, 0)
    assert saved == sum(len(s) - len(PH) for s in ("SECRET-1", "SECRET-2", "SECRET-3"))
    assert slo["response"]["choices"][0]["message"]["content"] == PH
    assert [d["b64_json"] for d in slo["response"]["data"]] == [PH, PH]
    assert slo["response"]["keep"] == "untouched"


# --------------------------------------------------------------------
# 11. 性能 / 递归安全 / 内存门禁
#     方案 D 要递归进容器，等于把"开销"从 O(1) 变成 O(子树规模)，
#     所以下面几条不是锦上添花，是这个选择能不能上线的判据。
# --------------------------------------------------------------------
def _time_scrub(build, path, repetitions=3):
    """
    测量"对一棵满载荷子树做首次清洗"的耗时。

    清洗是幂等的，第二趟是空操作，所以每一轮都必须用 build() 造一棵全新的树，
    并且先丢弃一轮结果作为预热，否则量到的是零写入的时间。
    """
    token_lists = [(path, cl._tokenize(path))]
    cl._scrub_slo_response({"response": build()}, token_lists)      # 预热
    best = float("inf")
    result = None
    for _ in range(repetitions):
        response = build()
        t0 = time.perf_counter()
        result = cl._scrub_slo_response({"response": response}, token_lists)
        best = min(best, time.perf_counter() - t0)
    return best, result


def test_scrub_is_in_place_no_whole_tree_copy():
    """原地改：容器对象身份不变、内存增量远小于载荷（深拷贝整树会 ≥ 载荷大小）。"""
    payload = "A" * 200_000
    resp = {"choices": [{"message": {"content": payload}}]}
    choice_id = id(resp["choices"][0])
    msg = resp["choices"][0]["message"]

    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    replaced, _capped, saved = cl._scrub_slo_response(
        {"response": resp},
        [("choices[*].message.content", cl._tokenize("choices[*].message.content"))],
    )
    grew = tracemalloc.get_traced_memory()[0] - before
    tracemalloc.stop()

    assert (replaced, saved) == (1, len(payload) - len(PH))
    assert id(resp["choices"][0]) == choice_id
    assert msg is resp["choices"][0]["message"] and msg["content"] == PH
    # 深拷贝这棵树至少要再分配 200KB；原地替换只是把旧串换成短串，
    # 净增长应当接近 0（这里给 tracemalloc 自身记账留出余量）。
    assert grew < len(payload) / 4, f"grew {grew} bytes"


def test_wide_array_scales_linearly():
    """1 万个元素的数组：必须线性（平方级会慢两个数量级），且一个不漏地换掉。"""
    width = 4096

    def build(n):
        return {"data": [{"b64_json": "Z" * width, "index": i} for i in range(n)]}

    t_small, (r_small, _c, _s) = _time_scrub(lambda: build(1000),
                                             "data[*].b64_json")
    assert r_small == 1000

    t_big, (r_big, capped_big, saved_big) = _time_scrub(lambda: build(10_000),
                                                        "data[*].b64_json")
    assert r_big == 10_000 and capped_big == 0
    assert saved_big == 10_000 * (width - len(PH))
    assert t_big < 2.0, f"10k elements took {t_big:.3f}s"
    # 规模 10 倍：线性应 ≈10 倍，平方会 ≈100 倍。放宽到 20 倍以避开调度噪声。
    assert t_big < max(t_small * 20, 0.05), f"{t_small:.4f} -> {t_big:.4f}"


def test_every_element_of_wide_array_is_replaced_not_just_first():
    """通配 [*] 递归时最容易写成"只处理第一项"：逐项校验值与长度。"""
    resp = {"data": [{"b64_json": f"SECRET{i}" * 100} for i in range(500)]}
    first, last = resp["data"][0]["b64_json"], resp["data"][-1]["b64_json"]
    cl._scrub_slo_response({"response": resp},
                           [("data[*].b64_json", cl._tokenize("data[*].b64_json"))])
    assert len(resp["data"]) == 500
    assert all(d["b64_json"] == PH for d in resp["data"])
    dumped = json.dumps(resp)
    assert first[:40] not in dumped and first[-40:] not in dumped
    assert last[:40] not in dumped and last[-40:] not in dumped


def test_pathologically_deep_payload_does_not_hit_recursion_limit():
    """5000 层嵌套：显式栈不得 RecursionError，超深部分必须报 capped 而不是静默漏。"""
    node = {"content": "SECRET"}
    for _ in range(5000):
        node = {"child": node}

    try:
        limit = sys.getrecursionlimit()
        t0 = time.perf_counter()
        replaced, capped, saved = cl._scrub_subtree(node)
        elapsed = time.perf_counter() - t0

        assert sys.getrecursionlimit() == limit    # 没被谁悄悄改过
        assert (replaced, saved) == (0, 0)         # 叶子在第 5000 层，够不着
        assert capped == 1                         # 但必须留下"没清洗完"的证据
        assert elapsed < 1.0

        # 对照：限深以内必须真的清洗到，证明 capped 不是"永远不换"的借口。
        # saved = len("SECRET") - len(PH) = 6 - 10 = -4：契约里"叶子比占位符短
        # 时那一处反而变大"的这一支，在真实 base64 载荷上不会发生。
        shallow = {"content": "SECRET"}
        for _ in range(cl.MAX_SCRUB_DEPTH - 2):
            shallow = {"child": shallow}
        assert cl._scrub_subtree(shallow) == (1, 0, len("SECRET") - len(PH))
    finally:
        # 逐层断开 child 再释放。整条 5000 层的链直接交给引用计数会在 C 层
        # 递归 dealloc（CPython 3.12+ 会抛 "recursion depth exceeded while
        # deallocating"）；那是测试自己的清理问题，不是被测代码的问题。
        cur = node
        while isinstance(cur, dict) and isinstance(cur.get("child"), dict):
            nxt = cur["child"]
            cur["child"] = None
            cur = nxt
        assert cur == {"content": "SECRET"} or cur == {"content": PH}


def test_hook_overhead_on_realistic_payload_is_small(patched_rules):
    """真实量级（约 1.5MB base64 + 常规字段）的单次 hook 开销必须在毫秒级。"""
    payload = "data:image/png;base64," + ("QmFzZTY0" * 250_000)

    def build():
        return {
            "id": "chatcmpl-x", "object": "chat.completion", "created": 1,
            "model": "gpt-4o", "usage": {"prompt_tokens": 1,
                                         "completion_tokens": 2},
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": payload}}],
        }

    logger_obj = cl.Base64ScrubLogger()

    def run_hook():
        kwargs = make_kwargs("https://provider-a.example.com/v1", build())
        asyncio.run(logger_obj.async_logging_hook(kwargs, None, "acompletion"))
        return kwargs["standard_logging_object"]["response"]

    run_hook()                                        # 预热
    t0 = time.perf_counter()
    resp = run_hook()
    elapsed = time.perf_counter() - t0

    assert resp["choices"][0]["message"]["content"] == PH
    assert len(payload) > 1_000_000
    assert elapsed < 0.05, f"hook took {elapsed:.4f}s on a {len(payload)}-byte payload"


def test_second_pass_is_free(patched_rules):
    """幂等不只是"值不变"，第二趟还必须是零写入、零额外分配（重复调用不放大开销）。"""
    resp = {"data": [{"b64_json": "Z" * 100_000} for _ in range(200)]}
    token_lists = [("data[*].b64_json", cl._tokenize("data[*].b64_json"))]
    first = cl._scrub_slo_response({"response": resp}, token_lists)
    assert first[0] == 200

    t0 = time.perf_counter()
    second = cl._scrub_slo_response({"response": resp}, token_lists)
    elapsed = time.perf_counter() - t0
    assert second == (0, 0, 0)
    assert elapsed < 0.5
