"""core/utils.py 的单元测试。"""

from core.utils import strip_code_fence


def test_strip_code_fence_plain():
    """无代码块包裹时应原样返回（仅 strip 首尾空白）。"""
    assert strip_code_fence('  {"a": 1}  ') == '{"a": 1}'


def test_strip_code_fence_json_fence():
    """带 ```json 包裹时应剥离围栏。"""
    assert strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_strip_code_fence_plain_fence():
    """带 ``` 无语言标记包裹时应剥离围栏。"""
    assert strip_code_fence('```\n{"a": 1}\n```') == '{"a": 1}'


def test_strip_code_fence_with_surrounding_text():
    """JSON 前后带说明文字时，应只保留围栏内的 JSON。"""
    text = '这是结果：\n```json\n{"a": 1}\n```\n完毕'
    assert strip_code_fence(text) == '{"a": 1}'


def test_strip_code_fence_unclosed():
    """只有开围栏没有闭合时原样返回，交由 json.loads 报错。"""
    text = '```json\n{"a": 1}'
    assert strip_code_fence(text) == text


def test_strip_code_fence_empty():
    """空字符串应原样返回空字符串。"""
    assert strip_code_fence("") == ""


def test_strip_code_fence_nested():
    """多层 ``` 时只剥离最外层，内部反引号保留。"""
    text = '```json\n{"a": "```"}\n```'
    assert strip_code_fence(text) == '{"a": "```"}'
