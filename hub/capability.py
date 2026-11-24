"""模型能力解析：上下文窗口 / 输出上限 / 模态。

为什么单独一层：
1. 各家 /models 返回结构差异极大。Packet 与 AMD 放在 `hai.context_length`，
   LightVela 干脆不返回（实测只有 id / owned_by / supported_endpoint_types）。
2. `auto` 这类别名不决定窗口——真正服务的是响应里 `model` 字段那个模型。
   窗口必须挂到「实际服务的模型」上，否则拿别名去比窗口毫无意义。

原则：拿不到就是 UNKNOWN，不猜、不用默认值填充。
"""
from __future__ import annotations

UNKNOWN = "UNKNOWN"

# 常见字段路径，按顺序探测
CONTEXT_PATHS = [
    ("context_length",),
    ("hai", "context_length"),
    ("context_window",),
    ("max_context_length",),
    ("max_input_tokens",),
    ("input_token_limit",),
]
OUTPUT_PATHS = [
    ("max_output_tokens",),
    ("hai", "max_output_tokens"),
    ("max_tokens",),
    ("output_token_limit",),
]
TOOLS_PATHS = [
    ("supports_tools",),
    ("hai", "supports_tools"),
    ("capabilities", "tools"),
]
MODALITY_PATHS = [
    ("modalities",),
    ("hai", "modalities"),
    ("input_modalities",),
]


def _dig(obj: dict, path: tuple):
    cur = obj
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _first(obj: dict, paths: list[tuple], cast=None):
    for p in paths:
        v = _dig(obj, p)
        if v is None:
            continue
        if cast:
            try:
                return cast(v)
            except Exception:
                continue
        return v
    return None


def parse_model_entry(m: dict) -> dict:
    """把一条 /models 条目归一化成统一结构。

    兼容三种形状：
      - OpenAI 极简：{id, owned_by}                     （LightVela，无窗口）
      - 厂商嵌套：  {id, hai:{context_length, ...}}      （本地桥 / Packet）
      - OpenRouter 风格：{id, context_length, architecture, supported_parameters, pricing}
    """
    mid = m.get("id") or m.get("model") or m.get("name")
    ctx = _first(m, CONTEXT_PATHS, int)
    # 实测 AMD 的 Clef / d3 / d3-flash 把 context_length 写成 0。
    # 0 不是"零上下文"，而是"未声明" —— 必须归为 UNKNOWN，否则会算出可用输入 0。
    if ctx == 0:
        ctx = None
    out = _first(m, OUTPUT_PATHS, int)
    tools = _first(m, TOOLS_PATHS)

    # modalities / 输出模态：OpenRouter 风格放在 architecture 里
    modalities = _first(m, MODALITY_PATHS)
    arch = m.get("architecture") if isinstance(m.get("architecture"), dict) else {}
    output_mods = arch.get("output_modalities") or m.get("output")
    if modalities is None and arch.get("input_modalities"):
        modalities = arch["input_modalities"]

    # 是否对话模型：输出模态含 text 才算。（d3-flash 输出是 decision）
    is_chat = None
    if isinstance(output_mods, list) and output_mods:
        is_chat = "text" in output_mods

    # supported_parameters 里带 tools 才算支持工具调用
    sp = m.get("supported_parameters")
    if tools is None and isinstance(sp, list):
        tools = "tools" in sp

    # 推理能力
    reasoning = None
    rt = m.get("hai", {}).get("reasoning") if isinstance(m.get("hai"), dict) else None
    if rt is not None:
        reasoning = bool(rt)
    elif isinstance(sp, list) and "reasoning_effort" in sp:
        reasoning = True
    else:
        name = str(mid or "").lower() + " " + str(m.get("name") or "").lower()
        if any(k in name for k in ("reasoning", "-r1", "thinking", "glm-", "o1", "o3")):
            reasoning = True

    pricing = m.get("pricing") if isinstance(m.get("pricing"), dict) else None
    is_free = m.get("free")
    if is_free is None and pricing:
        try:
            is_free = float(pricing.get("completion") or 0) == 0 and \
                      float(pricing.get("prompt") or 0) == 0
        except Exception:
            is_free = None

    return {
        "model_id": mid,
        "display_name": m.get("name"),
        "context_length": ctx,
        "context_source": "declared_api" if ctx is not None else UNKNOWN,
        "max_output_tokens": out,
        "supports_tools": None if tools is None else int(bool(tools)),
        "supports_reasoning": None if reasoning is None else int(bool(reasoning)),
        "modalities": ",".join(modalities) if isinstance(modalities, list) else None,
        "is_chat": None if is_chat is None else int(is_chat),
        "is_free": None if is_free is None else int(bool(is_free)),
    }


def usable_input_tokens(context_length: int | None,
                        max_output_tokens: int | None) -> int | None:
    """可用输入预算 = 上下文窗口 − 输出上限。

    很多服务商把输入和输出算在同一个窗口里，报"上下文 128K"时
    并不代表能塞 128K 输入。这个值才是真正能喂进去的量。
    """
    if context_length is None:
        return None
    if max_output_tokens is None:
        return context_length
    return max(0, context_length - max_output_tokens)


def fmt_tokens(n: int | None) -> str:
    if n is None:
        return UNKNOWN
    if n >= 1024:
        return f"{n // 1024}K" if n % 1024 == 0 else f"{n/1024:.1f}K"
    return str(n)
