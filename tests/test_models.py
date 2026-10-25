"""core/models.py 的单元测试。

覆盖各模型的 JSON 契约字段与序列化 / 反序列化往返。
"""

import json

from core.models import (
    ColumnInfo,
    ExecutionResult,
    GenerationRequest,
    GenerationResult,
    ParsedRequirement,
    SchemaInfo,
    TestGenerationResult,
    TestResult,
)


# JSON 契约规定的 GenerationResult 顶层字段
CONTRACT_FIELDS = {
    "requirement",
    "sql",
    "pandas",
    "tests",
    "assumptions",
    "questions",
    "risks",
}

# ExecutionResult 按模块 4 规格固定的字段
EXECUTION_RESULT_FIELDS = {
    "success",
    "columns",
    "rows",
    "row_count",
    "elapsed_ms",
    "error",
}


def test_generation_result_contract_fields():
    """GenerationResult 必须保留 JSON 契约的 7 个字段，并额外包含 success。"""
    assert CONTRACT_FIELDS.issubset(set(GenerationResult.model_fields))
    assert "success" in GenerationResult.model_fields


def test_generation_result_success_field():
    """success 默认 False，序列化后保持原值。"""
    assert GenerationResult().success is False

    result = GenerationResult(success=True, sql="SELECT 1")
    restored = GenerationResult.model_validate(json.loads(result.model_dump_json()))
    assert restored.success is True


def test_generation_request_round_trip():
    """GenerationRequest 序列化后应能原样反序列化。"""
    request = GenerationRequest(
        requirement="按月统计销售额",
        schema_text="CREATE TABLE t (month TEXT, amount DOUBLE)",
        sample_csv="month,amount\n2024-01,100\n",
        target="sql",
    )

    payload = json.loads(request.model_dump_json())
    restored = GenerationRequest.model_validate(payload)

    assert restored == request


def test_generation_result_round_trip():
    """GenerationResult 序列化后应能原样反序列化。"""
    result = GenerationResult(
        requirement="按月统计销售额",
        sql="SELECT month, SUM(amount) AS total -- 中文注释",
        pandas="",
        tests="",
        assumptions=["金额单位为元"],
        questions=["是否需要排除退款？"],
        risks=["样例数据量过小"],
    )

    payload = json.loads(result.model_dump_json())
    restored = GenerationResult.model_validate(payload)

    assert restored == result


def test_execution_result_fields_and_round_trip():
    """ExecutionResult 字段与模块 4 规格一致，且可序列化往返。"""
    assert set(ExecutionResult.model_fields) == EXECUTION_RESULT_FIELDS

    result = ExecutionResult(
        success=True,
        columns=["month", "total"],
        rows=[{"month": "2024-01", "total": 100.0}],
        row_count=1,
        elapsed_ms=5,
        error=None,
    )

    restored = ExecutionResult.model_validate(json.loads(result.model_dump_json()))

    assert restored == result


def test_test_result_round_trip():
    """TestResult 序列化后应能原样反序列化。"""
    result = TestResult(
        total=3,
        passed=2,
        failed=1,
        details=[{"name": "test_a", "status": "passed", "reason": ""}],
    )

    restored = TestResult.model_validate(json.loads(result.model_dump_json()))

    assert restored == result


def test_test_generation_result_round_trip():
    """TestGenerationResult 含嵌套 TestResult，也应能序列化往返。"""
    result = TestGenerationResult(
        content="def test_a():\n    assert True\n",
        attempts=2,
        final_status="failed",
        failure_reason="断言失败",
        suggestion="检查过滤条件",
        last_test_result=TestResult(total=1, passed=0, failed=1),
    )

    restored = TestGenerationResult.model_validate(
        json.loads(result.model_dump_json())
    )

    assert restored == result


def test_schema_info_round_trip():
    """SchemaInfo / ColumnInfo 序列化后应能原样反序列化，支持中文列名。"""
    schema = SchemaInfo(
        table_name="orders",
        columns=[
            ColumnInfo(name="订单号", dtype="TEXT"),
            ColumnInfo(name="金额", dtype="DOUBLE"),
        ],
    )

    restored = SchemaInfo.model_validate(json.loads(schema.model_dump_json()))

    assert restored == schema


def test_parsed_requirement_defaults_and_round_trip():
    """ParsedRequirement 默认值应为空，且可序列化往返。"""
    parsed = ParsedRequirement(
        metrics=["复购率"],
        dimensions=["month"],
        filters=["排除退款"],
        time_range="最近 12 个月",
        order_by="month",
        original_requirement="统计最近 12 个月复购率",
    )

    assert parsed.assumptions == []
    assert parsed.questions == []

    restored = ParsedRequirement.model_validate(json.loads(parsed.model_dump_json()))
    assert restored == parsed
    assert restored.original_requirement == "统计最近 12 个月复购率"

    empty = ParsedRequirement()
    assert empty.time_range is None
    assert empty.order_by is None
    assert empty.original_requirement == ""
