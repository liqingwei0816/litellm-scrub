"""
变异测试（mutation testing）：证明测试套件不是"跑绿的装饰"。

做法：对 custom_logger.py 逐条施加"语义变异"（每条都对应契约里的一句话），
在隔离目录里用变异体替换原实现，跑整套测试；只要有一条测试失败就算"杀死"。
存活（SURVIVED）的变异 = 契约里有一条没有任何测试在守 -> 必须补测试。

为什么要这个：覆盖率只说明"这行被执行过"，不说明"这行被写错时测试会红"。
本项目最贵的失效模式恰恰是静默的——某个分支被改坏后测试仍然全绿，
敏感数据就带着"已清洗"的假象入库。变异测试是唯一能证伪这一点的门禁。

用法（在钉死的 litellm 镜像内跑，deps 目录里是解包好的 pytest/hypothesis）：

    docker run --rm -v <repo>:/work -v <deps>:/deps -w /work \
        -e PYTHONPATH=/deps -e SCRUB_RULES_PATH=/work/scrub_rules.yaml \
        --entrypoint /app/.venv/bin/python3 \
        ghcr.io/berriai/litellm:v1.83.14-stable tests/mut_harness.py

只跑指定几条：在命令末尾追加编号，如 `tests/mut_harness.py M01 M16`。
退出码：0 = 全部杀死且控制组全绿；1 = 有存活/锚点失效；2 = 控制组不绿。
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

# 仓库根 = 本文件所在目录（tests/）的上一级；不写死 /work，
# 这样在宿主机上直接跑（只要 python 环境里有 litellm 和 pytest）也能用。
REPO = pathlib.Path(__file__).resolve().parent.parent
ROOT = pathlib.Path(os.environ.get("MUT_ROOT")
                    or tempfile.mkdtemp(prefix="mutants-"))

SRC = "custom_logger.py"

# (编号, 契约条款, 原文锚点, 变异后)
# 锚点必须在源码里"恰好出现一次"：harness 会先数命中次数，不是 1 次就判为
# INVALID 而不是假装杀死——重构挪动代码后最容易出现的正是锚点悄悄失效。
MUTATIONS = [
    ("M01", "命中即替换：字符串叶子必须被换成占位符",
     "                    node[key] = SCRUB_PLACEHOLDER\n                    replaced += 1",
     "                    node[key] = value\n                    replaced += 1"),

    ("M02", "结构守恒：绝不删除键（旧删除语义回归）",
     "                    node[key] = SCRUB_PLACEHOLDER\n                    replaced += 1",
     "                    del node[key]\n                    replaced += 1"),

    ("M03", "类型守恒：非字符串叶子原样保留",
     "            # 其它类型：非字符串叶子，按契约原样保留\n    return replaced, capped, saved",
     "            else:\n                node[key] = SCRUB_PLACEHOLDER\n"
     "                replaced += 1\n    return replaced, capped, saved"),

    ("M04", "限深：超出 MAX_SCRUB_DEPTH 必须截停并报 capped",
     "                if depth + 1 > MAX_SCRUB_DEPTH:",
     "                if depth + 1 > MAX_SCRUB_DEPTH * 1000:"),

    ("M05", "幂等且不虚报：已是占位符的叶子不计入 replaced",
     "                if value != SCRUB_PLACEHOLDER:",
     "                if True:"),

    ("M06", "体积记账：saved 是净节省字符数（可观测性不得说谎）",
     "                    saved += len(value) - len(SCRUB_PLACEHOLDER)",
     "                    saved += len(SCRUB_PLACEHOLDER) - len(value)"),

    ("M07", "终点通配：[*] 必须逐项处理，不能只做第一项",
     "            for i in range(len(obj)):\n"
     "                obj[i], n, c, s = _scrub_value(obj[i])",
     "            for i in range(min(1, len(obj))):\n"
     "                obj[i], n, c, s = _scrub_value(obj[i])"),

    ("M08", "中间通配：[*].x 必须对每一项下钻",
     "            for item in obj:\n                n, c, s = _scrub_path(item, rest)",
     "            for item in obj[:1]:\n                n, c, s = _scrub_path(item, rest)"),

    ("M09", "容器终点：容器本身原样放回（只换里面的字符串）",
     "        replaced, capped, saved = _scrub_subtree(value)\n"
     "        return value, replaced, capped, saved",
     "        replaced, capped, saved = _scrub_subtree(value)\n"
     "        return None, replaced, capped, saved"),

    ("M10", "归一化：api_base 比较大小写不敏感",
     '    return base.strip().lower().rstrip("/")',
     '    return base.strip().rstrip("/")'),

    ("M11", "安全边界：前缀之后必须是 / : ? # 或结尾（防异域名误命中）",
     '            if rest == "" or rest[0] in ("/", ":", "?", "#"):',
     "            if True:"),

    ("M12", "客户端不受影响：result 必须原样透传（连浅拷贝都不行）",
     "        return kwargs, result\n\n    def logging_hook(self, kwargs, result, call_type):",
     "        return kwargs, (dict(result) if isinstance(result, dict) else result)\n\n"
     "    def logging_hook(self, kwargs, result, call_type):"),

    ("M13", "api_base 取值优先级：路由器写入的 metadata.api_base 最优先",
     "        # 1) 最可靠：路由器写入的 metadata.api_base\n        metadata.get(\"api_base\"),",
     "        # 1) 最可靠：路由器写入的 metadata.api_base\n        None,"),

    ("M14", "可观测性：命中规则却零替换必须告警",
     "    if replaced == 0:\n        logger.warning(\n"
     '            "scrub rule matched but replaced nothing "',
     "    if False:\n        logger.warning(\n"
     '            "scrub rule matched but replaced nothing "'),

    ("M15", "两条路径日志必须一致：兜底路径同样要报'清洗没覆盖全'",
     "    if capped:\n        logger.warning(\n"
     '            "scrub stopped at %d container(s) beyond MAX_SCRUB_DEPTH=%d; "',
     '    if capped and origin != "sync":\n        logger.warning(\n'
     '            "scrub stopped at %d container(s) beyond MAX_SCRUB_DEPTH=%d; "'),

    ("M16", "原地改写：不得复制整棵响应树（内存/耗时门禁）",
     "        n, c, s = _scrub_path(response, tokens)",
     "        import copy\n        n, c, s = _scrub_path(copy.deepcopy(response), tokens)"),

    ("M17", "同步钩子必须真的清洗（不是只留个签名）",
     "        try:\n            _scrub_and_report(kwargs, \"sync\")",
     "        try:\n            _scrub_and_report(kwargs, \"sync\") if False else None"),

    ("M18", "配置项非法值 fail-open：负数必须退回默认并告警",
     "    if value < 0:",
     "    if False:"),

    ("M19", "重复前缀必须报 ERROR（第二条规则的 scrub_paths 永不生效）",
     "            if prefix in seen_prefixes:",
     "            if False:"),

    ("M20", "kwargs 的每一层都不是 dict 时不得抛异常",
     "    return value if isinstance(value, dict) else {}",
     "    return value"),

    ("M21", "response 形状不符必须指名道姓地告警",
     "    if not isinstance(response, dict):\n        if response is not None:",
     "    if not isinstance(response, dict):\n        if False:"),

    ("M22", "一条规则下的每个 scrub_path 都必须执行",
     "    for _raw_path, tokens in token_lists:",
     "    for _raw_path, tokens in token_lists[:1]:"),
]

PYTEST = [sys.executable, "-m", "pytest", "tests/test_custom_logger.py",
          "-x", "-q", "--no-header", "-p", "no:cacheprovider"]


def build(name, old, new):
    """在一个隔离目录里造出"只改了这一处"的变异体，返回 (目录, 错误信息)。"""
    d = ROOT / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    shutil.copy(REPO / "scrub_rules.yaml", d / "scrub_rules.yaml")
    shutil.copytree(REPO / "tests", d / "tests",
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    src = (REPO / SRC).read_text(encoding="utf-8")
    hits = src.count(old)
    if hits != 1:
        return d, f"ANCHOR-NOT-UNIQUE ({hits} hits)"
    (d / SRC).write_text(src.replace(old, new, 1), encoding="utf-8")
    return d, None


def main():
    # 刻意不继承 os.environ：容器里的 PYTHONPATH/HOME 需要精确控制，
    # 而 SCRUB_RULES_PATH 每组都要换成变异体自己那份，绝不能沿用外面的。
    env = {"PYTHONPATH": os.environ.get("PYTHONPATH", ""),
           "HYPOTHESIS_STORAGE_DIRECTORY": os.environ.get("HYPOTHESIS_STORAGE_DIRECTORY",
                                                          str(ROOT / "hyp")),
           "HOME": os.environ.get("HOME", str(ROOT)),
           "PYTHONDONTWRITEBYTECODE": "1",
           "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    killed, survived, invalid = [], [], []

    print("==== 控制组：未变异的原实现 ====", flush=True)
    ctrl = subprocess.run(
        PYTEST, cwd=REPO,
        env={**env, "SCRUB_RULES_PATH": str(REPO / "scrub_rules.yaml")},
        capture_output=True, text=True)
    print(ctrl.stdout.strip().splitlines()[-1] if ctrl.stdout else "", flush=True)
    if ctrl.returncode != 0:
        print("CONTROL NOT GREEN -> 变异结果无意义，中止。")
        print(ctrl.stdout[-3000:], ctrl.stderr[-2000:])
        return 2

    selected = set(sys.argv[1:])
    for name, clause, old, new in MUTATIONS:
        if selected and name not in selected:
            continue
        d, err = build(name, old, new)
        if err:
            invalid.append((name, clause, err))
            print(f"[{name}] {err}  {clause}", flush=True)
            continue
        env["SCRUB_RULES_PATH"] = str(d / "scrub_rules.yaml")
        p = subprocess.run(PYTEST, cwd=d, env=env, capture_output=True, text=True)
        tail = [ln for ln in p.stdout.splitlines() if ln.startswith("FAILED")
                or " passed" in ln or " failed" in ln or "error" in ln.lower()]
        if p.returncode == 0:
            survived.append((name, clause))
            mark = "SURVIVED"
        else:
            killed.append((name, clause))
            mark = "KILLED"
        print(f"[{name}] {mark}  {clause}\n        {tail[-1].strip() if tail else ''}",
              flush=True)

    total = len(killed) + len(survived)
    expected = len(selected) if selected else len(MUTATIONS)
    print("\n==== 变异测试结果 ====")
    print(f"killed {len(killed)}/{total}"
          + (f"  (invalid anchors: {len(invalid)})" if invalid else ""))
    for name, clause in survived:
        print(f"  SURVIVED {name}: {clause}")
    for name, clause, err in invalid:
        print(f"  INVALID {name}: {clause} -> {err}")
    return 1 if (survived or invalid or len(killed) != expected) else 0


if __name__ == "__main__":
    sys.exit(main())
