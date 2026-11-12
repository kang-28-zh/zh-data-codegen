"""通用工具函数。

当前仅包含 strip_code_fence：剥离 LLM 返回中的 ``` 代码块围栏。
"""


def strip_code_fence(text: str) -> str:
    """剥离 ``` 代码块包裹，只保留最外层围栏内的内容。"""
    stripped = text.strip()
    if "```" not in stripped:
        return stripped

    start = stripped.find("```")
    end = stripped.rfind("```")
    if start == end:
        # 只有开围栏没有闭合，原样返回，交给 json.loads 报错
        return stripped

    newline = stripped.find("\n", start)
    if newline == -1:
        return stripped
    return stripped[newline + 1 : end].strip()
