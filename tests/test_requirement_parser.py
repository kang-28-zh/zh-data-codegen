"""core/requirement_parser.py 的单元测试。

全部用例 patch core.llm.call_llm，不发起真实网络请求，不使用真实 API Key。
"""

# 文件结构：① mock 数据区 ② _build_prompt ③ _build_retry_prompt
#           ④ _extract_tokens ⑤ _check_fields ⑥ parse_requirement ⑦ 超时路径

import pytest

from core import llm
from core.models import ColumnInfo, ParsedRequirement, SchemaInfo
from core.requirement_parser import (
    FIELD_TEMPLATES,
    _build_prompt,
    _build_retry_prompt,
    _check_fields,
    _extract_tokens,
    parse_requirement,
)


SCHEMA = SchemaInfo(
    table_name="sales",
    columns=[
        ColumnInfo(name="order_id", dtype="TEXT"),
        ColumnInfo(name="sale_name", dtype="TEXT"),
        ColumnInfo(name="amount", dtype="DOUBLE"),
        ColumnInfo(name="status", dtype="TEXT"),
        ColumnInfo(name="order_time", dtype="TIMESTAMP"),
    ],
)

SCHEMA_CN = SchemaInfo(
    table_name="订单表",
    columns=[
        ColumnInfo(name="订单号", dtype="TEXT"),
        ColumnInfo(name="销售", dtype="TEXT"),
        ColumnInfo(name="销售额", dtype="DOUBLE"),
        ColumnInfo(name="状态", dtype="TEXT"),
        ColumnInfo(name="订单日期", dtype="DATE"),
    ],
)


# 模拟「按销售统计成交额，排除退款」的正常返回；断言 metrics / filters / time_range 解析正确。
MOCK_NORMAL_JSON = """{
  "metrics": ["amount"],
  "dimensions": ["sale_name"],
  "filters": ["status != 'refund'"],
  "time_range": "2024 年",
  "order_by": "sale_name ASC",
  "assumptions": ["成交额按 amount 列汇总"],
  "questions": []
}"""

# 模拟真实中文 SQL 需求「统计每月每个销售销售额，排除退款」；
# 断言中文列名「销售额」保留在 metrics、「状态 != '退款'」保留在 filters。
MOCK_SQL_CN_JSON = """{
  "metrics": ["销售额"],
  "dimensions": ["销售", "月份"],
  "filters": ["状态 != '退款'"],
  "time_range": "2024 年",
  "order_by": "月份 ASC",
  "assumptions": ["销售额按 销售额 列汇总"],
  "questions": []
}"""

# 模拟真实中文 Pandas 需求「清洗订单表，排除退款，按销售汇总销售额并降序」；
# 断言聚合与过滤条件被正确解析。
MOCK_PANDAS_CN_JSON = """{
  "metrics": ["销售额"],
  "dimensions": ["销售"],
  "filters": ["状态 != '退款'"],
  "time_range": null,
  "order_by": "销售额 DESC",
  "assumptions": ["按销售分组汇总"],
  "questions": []
}"""

# 模拟模型漏返回字段（缺 time_range / order_by / assumptions / questions）；断言会报错。
MOCK_MISSING_FIELD_JSON = """{
  "metrics": ["amount"],
  "dimensions": ["sale_name"],
  "filters": []
}"""

# 模拟需求里提到 schema 不存在的字段 refund_amount；断言被移入 questions 并给出最接近建议。
MOCK_UNKNOWN_FIELD_JSON = """{
  "metrics": ["amount"],
  "dimensions": ["sale_name"],
  "filters": ["refund_amount > 100"],
  "time_range": null,
  "order_by": null,
  "assumptions": [],
  "questions": []
}"""


def _patch_llm(monkeypatch, *responses):
    """patch call_llm：单个响应时每次调用都返回它，多个响应时按顺序返回。"""
    if len(responses) == 1:
        monkeypatch.setattr(llm, "call_llm", lambda prompt: responses[0])
        return
    iterator = iter(responses)
    monkeypatch.setattr(llm, "call_llm", lambda prompt: next(iterator))


def test_extract_tokens_skips_single_quoted_literal():
    """status != 'refund' 只提取 status，不提取 refund。"""
    assert _extract_tokens("status != 'refund'") == ["status"]


def test_extract_tokens_skips_in_list_literals():
    """status IN ('paid', 'refund') 不提取 paid / refund 字面量。"""
    tokens = _extract_tokens("status IN ('paid', 'refund')")

    assert "status" in tokens
    assert "paid" not in tokens
    assert "refund" not in tokens


def test_extract_tokens_handles_quoted_identifier():
    """双引号包裹的字段名与无引号字段名都应被识别。"""
    assert _extract_tokens('"refund_amount" > 100')[0] == "refund_amount"
    assert _extract_tokens("refund_amount > 100")[0] == "refund_amount"


def test_build_prompt_contains_schema_and_rules():
    """prompt 必须含表名、列名、只输出 JSON 约束和 7 个字段模板，且不含样例数据。"""
    prompt = _build_prompt("统计每月销售额", SCHEMA)

    assert "sales" in prompt
    assert "order_id" in prompt
    assert "amount(DOUBLE)" in prompt
    assert "只输出 JSON" in prompt
    for template in FIELD_TEMPLATES:
        assert template in prompt
    assert "样例数据" not in prompt


def test_build_retry_prompt_truncates():
    """重试 prompt 应截断上次返回与错误信息，超长时丢弃上次返回。"""
    result = _build_retry_prompt("基础 prompt", "x" * 5000, "e" * 1000)

    assert "…（已截断）" in result
    assert "e" * 500 in result
    assert "e" * 501 not in result
    assert len(result) <= 8000

    fallback = _build_retry_prompt("o" * 7000, "x" * 2000, "e" * 10)
    assert "x" not in fallback
    assert len(fallback) <= 8000


def test_check_fields_known_fields_untouched():
    """字段都能在 schema 找到时，不应产生 questions。"""
    parsed = ParsedRequirement(metrics=["amount"], filters=["status != 'refund'"])

    result = _check_fields(parsed, SCHEMA)

    assert result.metrics == ["amount"]
    assert result.filters == ["status != 'refund'"]
    assert result.questions == []


def test_check_fields_unknown_moves_to_questions():
    """找不到的字段应移入 questions，并给出最接近字段建议，且不修改入参。"""
    parsed = ParsedRequirement(metrics=["amount"], filters=["refund_amount > 100"])

    result = _check_fields(parsed, SCHEMA)

    assert result.filters == []
    assert any("未找到字段 refund_amount" in q for q in result.questions)
    assert any("是否指 amount" in q for q in result.questions)
    # 入参保持不变
    assert parsed.filters == ["refund_amount > 100"]


def test_parse_requirement_normal(monkeypatch):
    """正常 JSON 一次解析成功。"""
    _patch_llm(monkeypatch, MOCK_NORMAL_JSON)

    parsed = parse_requirement("统计每个销售的成交额，排除退款", SCHEMA)

    assert parsed.metrics == ["amount"]
    assert parsed.dimensions == ["sale_name"]
    assert parsed.filters == ["status != 'refund'"]
    assert parsed.time_range == "2024 年"
    assert parsed.order_by == "sale_name ASC"
    assert parsed.questions == []
    assert parsed.original_requirement == "统计每个销售的成交额，排除退款"


def test_parse_requirement_retry_success(monkeypatch):
    """首次返回非法 JSON，第二次返回合法 JSON 时应重试成功。"""
    calls = {"count": 0}

    def fake_call(prompt):
        calls["count"] += 1
        return "这不是 JSON" if calls["count"] == 1 else MOCK_NORMAL_JSON

    monkeypatch.setattr(llm, "call_llm", fake_call)

    parsed = parse_requirement("统计成交额", SCHEMA)

    assert calls["count"] == 2
    assert parsed.metrics == ["amount"]


def test_parse_requirement_two_invalid_raises(monkeypatch):
    """两次都返回非法 JSON 时应抛出 ValueError，且消息带模块前缀。"""
    _patch_llm(monkeypatch, "这不是 JSON", "还是不是 JSON")

    with pytest.raises(ValueError) as exc_info:
        parse_requirement("统计成交额", SCHEMA)

    message = str(exc_info.value)
    assert "[requirement_parser]" in message
    assert "模型两次返回非法 JSON" in message


def test_parse_requirement_missing_field_raises(monkeypatch):
    """模型漏返回必需字段时应报错。"""
    _patch_llm(monkeypatch, MOCK_MISSING_FIELD_JSON)

    with pytest.raises(ValueError) as exc_info:
        parse_requirement("统计成交额", SCHEMA)

    message = str(exc_info.value)
    assert "[requirement_parser]" in message
    assert "缺少字段" in message


def test_parse_requirement_unknown_field_goes_to_questions(monkeypatch):
    """需求里的字段在 schema 找不到时，应移入 questions 而不是报错。"""
    _patch_llm(monkeypatch, MOCK_UNKNOWN_FIELD_JSON)

    parsed = parse_requirement("统计退款金额", SCHEMA)

    assert parsed.filters == []
    assert any("refund_amount" in question for question in parsed.questions)
    assert any("是否指 amount" in question for question in parsed.questions)


def test_parse_requirement_real_sql_sample(monkeypatch):
    """真实中文 SQL 需求样例：断言具体指标与过滤条件。"""
    _patch_llm(monkeypatch, MOCK_SQL_CN_JSON)

    parsed = parse_requirement("统计每月每个销售销售额，排除退款", SCHEMA_CN)

    assert parsed.metrics == ["销售额"]
    assert parsed.dimensions == ["销售", "月份"]
    assert parsed.filters == ["状态 != '退款'"]
    assert parsed.time_range == "2024 年"
    assert parsed.questions == []


def test_parse_requirement_real_pandas_sample(monkeypatch):
    """真实中文 Pandas 需求样例：断言指标、维度、过滤与排序。"""
    _patch_llm(monkeypatch, MOCK_PANDAS_CN_JSON)

    parsed = parse_requirement(
        "清洗订单表，排除退款，按销售汇总销售额并降序", SCHEMA_CN
    )

    assert parsed.metrics == ["销售额"]
    assert parsed.dimensions == ["销售"]
    assert parsed.filters == ["状态 != '退款'"]
    assert parsed.time_range is None
    assert parsed.order_by == "销售额 DESC"
    assert parsed.questions == []


def test_parse_requirement_api_timeout(monkeypatch):
    """call_llm 抛 APITimeoutError 时应转为带模块前缀的 ValueError。"""
    fake_error = type("APITimeoutError", (Exception,), {})()

    def raise_timeout(prompt):
        raise fake_error

    monkeypatch.setattr(llm, "call_llm", raise_timeout)

    with pytest.raises(ValueError) as exc_info:
        parse_requirement("统计成交额", SCHEMA)

    message = str(exc_info.value)
    assert "[requirement_parser]" in message
    assert "模型调用超时" in message


def test_parse_requirement_builtin_timeout(monkeypatch):
    """call_llm 抛内置 TimeoutError 时也应转为带模块前缀的 ValueError。"""

    def raise_timeout(prompt):
        raise TimeoutError("timeout")

    monkeypatch.setattr(llm, "call_llm", raise_timeout)

    with pytest.raises(ValueError) as exc_info:
        parse_requirement("统计成交额", SCHEMA)

    message = str(exc_info.value)
    assert "[requirement_parser]" in message
    assert "模型调用超时" in message
