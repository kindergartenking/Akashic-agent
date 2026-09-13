"""M0 最小验证脚本。

验证 M0 平移的四个契约是否与原版语义一致：
1. Plugin 子类自动注册（__init_subclass__ -> plugin_registry）
2. @on_* / @tool / @on_tool_pre 装饰器写入正确的 MetadataKind / event / handler_type
3. Tool ABC 的 __init_subclass__ 校验 + validate_params + to_schema
4. EventBus 四语义：emit(GATE 拦截链) / observe(TAP 串行) / fanout(TAP 并行) / enqueue+drain(后台队列)
5. 快照版本切换：handler 集合随 _current 指针切换（turn-boundary rollout，PLG-013）

运行方式（项目根目录）：
    <venv python> backend/m0_verify.py
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

# 保证 backend 在 sys.path 上（与 README 的 PYTHONPATH=backend 约定一致）
BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agent.plugins import Plugin, on_after_turn, on_before_turn, on_tool_pre, tool  # noqa: E402
from agent.plugins.registry import (  # noqa: E402
    HandlerType,
    MetadataKind,
    PluginEventType,
    plugin_registry,
)
from agent.plugins.snapshot import (  # noqa: E402
    RuntimeSnapshot,
    RuntimeSnapshotStore,
    bind_runtime_snapshot,
    reset_runtime_snapshot,
)
from agent.tools.base import Tool  # noqa: E402
from bus.event_bus import EventBus  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ─────────────────────────────────────────────────────────────────────────────
# 1 + 2 + 3：插件 / 装饰器 / Tool 契约
# ─────────────────────────────────────────────────────────────────────────────
section("1. Plugin 子类自动注册")


class DemoPlugin(Plugin):
    name = "demo"

    @on_before_turn(priority=10)
    async def before_turn(self, event: object) -> object:
        return event

    @on_after_turn()
    async def after_turn(self, event: object) -> None:
        return None

    @on_tool_pre(tool_name="add")
    async def pre_add(self, event: object) -> None:
        return None

    @tool("add", risk="read-only")
    async def add(self, event: object, a: int, b: int = 0) -> str:
        """Add two numbers.

        :param a: 第一个数
        :param b: 第二个数
        """
        return str(a + b)


check("Plugin 类已进入 plugin_registry._classes", plugin_registry.get_class(DemoPlugin.__module__) is not None)

handlers = plugin_registry.get_handlers_by_module_path(DemoPlugin.__module__)

# before_turn -> LIFECYCLE / BEFORE_TURN / GATE
bt = [h for h in handlers if h.handler_name == "before_turn"]
check("before_turn 注册为 LIFECYCLE+BEFORE_TURN+GATE", len(bt) == 1 and bt[0].kind is MetadataKind.LIFECYCLE and bt[0].event_type is PluginEventType.BEFORE_TURN and bt[0].handler_type is HandlerType.GATE)
check("before_turn 携带 priority=10", bt and bt[0].priority == 10)

# after_turn -> TAP
at = [h for h in handlers if h.handler_name == "after_turn"]
check("after_turn 注册为 LIFECYCLE+AFTER_TURN+TAP", at and at[0].kind is MetadataKind.LIFECYCLE and at[0].event_type is PluginEventType.AFTER_TURN and at[0].handler_type is HandlerType.TAP)

# tool -> TOOL
ad = [h for h in handlers if h.handler_name == "add"]
check("add 注册为 MetadataKind.TOOL（不走 EventBus）", ad and ad[0].kind is MetadataKind.TOOL and ad[0].tool_name == "add" and ad[0].tool_risk == "read-only")
# 注：本模块带 `from __future__ import annotations`，annotation 被延迟为字符串；
# 原版 _derive_params_schema 用 getattr(ann, "__name__") 解析，字符串没有 __name__，
# 故类型恒落 "string"（忠实复刻原版行为，非复刻 bug）。
expected_schema = {"type": "object", "properties": {"a": {"type": "string", "description": "第一个数"}, "b": {"type": "string", "description": "第二个数"}}, "required": ["a"]}
check("@tool 派生了参数 schema（含 annotation 延迟解析 quirk）", ad and ad[0].tool_schema == expected_schema, (f"got={ad[0].tool_schema!r}" if ad else "no handler"))

# on_tool_pre -> TOOL_HOOK
pt = [h for h in handlers if h.handler_name == "pre_add"]
check("pre_add 注册为 MetadataKind.TOOL_HOOK", pt and pt[0].kind is MetadataKind.TOOL_HOOK and pt[0].hook_tool_name == "add")


section("2. Tool ABC 校验")


def _define_bad_tool() -> None:
    class BadTool(Tool):
        async def execute(self, **kwargs: object) -> str:
            return "x"


try:
    _define_bad_tool()
    check("缺失字段的 Tool 抛 TypeError", False)
except TypeError as exc:
    check("缺失字段的 Tool 抛 TypeError", "必须定义字段" in str(exc))


class EchoTool(Tool):
    name = "echo"
    description = "回显输入"
    parameters = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "required": ["x"],
    }

    async def execute(self, **kwargs: object) -> str:
        return str(kwargs.get("x", ""))


check("validate_params 合法参数通过", EchoTool().validate_params({"x": "hi"}) == [])
check("validate_params 缺必填报错", EchoTool().validate_params({}) != [])
check("to_schema 产出 OpenAI function 格式", EchoTool().to_schema()["function"]["name"] == "echo")


# ─────────────────────────────────────────────────────────────────────────────
# 4：EventBus 四语义
# ─────────────────────────────────────────────────────────────────────────────
section("3. EventBus emit / observe / fanout / enqueue")


@dataclass
class Msg:
    text: str


async def _test_event_bus() -> None:
    bus = EventBus()

    # emit：GATE 拦截链，返回新事件替换当前事件
    async def gate1(e: Msg) -> Msg:
        return Msg(text=e.text + "|gate1")

    async def gate2(e: Msg) -> Msg:
        return Msg(text=e.text + "|gate2")

    bus.on(Msg, gate1)
    bus.on(Msg, gate2)
    out = await bus.emit(Msg("hi"))
    check("emit 依次跑 GATE 链并替换事件", out.text == "hi|gate1|gate2", f"got={out.text!r}")

    # observe：TAP 串行，返回值忽略，单个失败不打断
    bus2 = EventBus()
    seen: list[str] = []

    async def tap1(e: Msg) -> None:
        seen.append("t1:" + e.text)

    async def boom(e: Msg) -> None:
        raise RuntimeError("boom")

    async def tap2(e: Msg) -> None:
        seen.append("t2:" + e.text)

    bus2.on(Msg, tap1)
    bus2.on(Msg, boom)
    bus2.on(Msg, tap2)
    await bus2.observe(Msg("x"))
    check("observe 串行且单个失败不打断", seen == ["t1:x", "t2:x"], f"got={seen!r}")

    # fanout：TAP 并行
    bus3 = EventBus()
    order: list[str] = []

    async def slow(e: Msg) -> None:
        await asyncio.sleep(0.02)
        order.append("slow")

    async def fast(e: Msg) -> None:
        order.append("fast")

    bus3.on(Msg, slow)
    bus3.on(Msg, fast)
    await bus3.fanout(Msg("y"))
    check("fanout 并行执行（fast 先于 slow 完成）", order == ["fast", "slow"], f"got={order!r}")

    # enqueue + drain：后台队列
    bus4 = EventBus()
    queued: list[str] = []

    async def qtap(e: Msg) -> None:
        queued.append(e.text)

    bus4.on(Msg, qtap)
    bus4.enqueue(Msg("q1"))
    bus4.enqueue(Msg("q2"))
    await bus4.drain()
    check("enqueue 后台队列经 fanout 消费", queued == ["q1", "q2"], f"got={queued!r}")
    await bus4.aclose()


asyncio.run(_test_event_bus())


# ─────────────────────────────────────────────────────────────────────────────
# 5：快照版本切换（handler 集合随 _current 指针切换）
# ─────────────────────────────────────────────────────────────────────────────
section("4. 快照版本切换（turn-boundary rollout）")


async def _test_snapshot_swap() -> None:
    bus = EventBus()
    store = RuntimeSnapshotStore()
    bus.bind_runtime_snapshot_store(store)

    calls: list[str] = []

    async def hA(e: Msg) -> Msg:
        calls.append("A")
        return e

    async def hB(e: Msg) -> Msg:
        calls.append("B")
        return e

    # v1：handler 集合 = {A}
    snap1 = RuntimeSnapshot(snapshot_id="s1", event_handlers={Msg: (hA,)})
    store.install(snap1)

    await bus.emit(Msg("turn1"))
    check("v1 阶段 emit 命中 handler A", calls == ["A"], f"got={calls!r}")

    # 一个"旧 turn"在发布前先租下 v1 的 lease（模拟仍在运行的旧 turn）
    lease1 = store.lease()

    # 发布 v2：handler 集合 = {B}
    calls.clear()
    snap2 = RuntimeSnapshot(snapshot_id="s2", event_handlers={Msg: (hB,)})
    tx = store.begin_publish(snap2)
    await store.commit(tx)

    # 新 turn（无绑定）命中 B
    await bus.emit(Msg("turn2-new"))
    check("v2 阶段新 turn 命中 handler B", calls == ["B"], f"got={calls!r}")

    # 仍持有 v1 lease 的"旧 turn"（显式绑定）继续命中 A（turn 边界 rollout）
    calls.clear()
    token = bind_runtime_snapshot(lease1)
    try:
        await bus.emit(Msg("turn2-old"))
    finally:
        reset_runtime_snapshot(token)
    check("旧 turn 仍命中 v1 的 handler A（PLG-013）", calls == ["A"], f"got={calls!r}")

    await lease1.release()
    await store.close()


asyncio.run(_test_snapshot_swap())


# ─────────────────────────────────────────────────────────────────────────────
section("结果")
print(f"PASS={PASS}  FAIL={FAIL}")
if FAIL:
    sys.exit(1)
print("M0 验证通过。")
