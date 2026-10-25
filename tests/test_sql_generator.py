"""core/sql_generator.py 的单元测试。

全部用例 patch core.llm.call_llm，不发起真实网络请求。
"""

# 文件结构：① mock 数据区 ② _build_sql_prompt ③ 正常生成 ④ 护栏拦截
#           ⑤ 执行失败 ⑥ markdown 剥离 ⑦ 中文列名 ⑧ call_llm 异常

import pytest

import core.sql_generator as sql_generator_module
from core import llm
from core.models import ColumnInfo, ParsedRequirement, SchemaInfo
from core.sql_generator import _build_sql_prompt, generate_sql


SCHEMA = SchemaInfo(
    table_name="orders",
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

PARSED = ParsedRequirement(
    metrics=["amount"],
    dimensions=["sale_name"],
    filters=["status = 'paid'"],
    time_range="2024 年",
    assumptions=["金额单位为元"],
    questions=["是否排除退款？"],
    original_requirement="统计每个销售的成交额，排除退款",
)

PARSED_CN = ParsedRequirement(
    metrics=["销售额"],
    dimensions=["销售"],
    filters=["状态 != '退款'"],
    assumptions=["金额单位为元"],
    questions=[],
    original_requirement="统计每个销售的销售额，排除退款",
)

SAMPLE_CSV = (
    "order_id,sale_name,amount,status\n"
    "1,张三,100,paid\n"
    "2,李四,200,refund\n"
    "3,王五,300,paid\n"
    "4,赵六,400,paid\n"
)

SAMPLE_CN = "订单号,销售,销售额,状态\n1,张三,100,已支付\n2,李四,200,退款\n"

# 超长样例：第 1 行数据就超过 500 字符，第 2 行应被整体丢弃，不能出现半行
SAMPLE_LONG_CSV = "order_id,payload\n1," + "9" * 600 + "\n2,UNIQUE_MARKER\n"

# 模拟正常 SQL：按销售汇总金额，过护栏并执行成功
MOCK_VALID_SQL = (
    "SELECT sale_name, SUM(amount) AS total_amount FROM t "
    "WHERE status = 'paid' GROUP BY sale_name"
)

# 模拟含 DROP 的 SQL：必须被 sql_guard 拦截
MOCK_DROP_SQL = "DROP TABLE t"

# 模拟引用不存在字段：必须被 sql_guard 拦截
MOCK_UNKNOWN_COLUMN_SQL = "SELECT nonexistent_column FROM t"

# 模拟能过 sql_guard 但 DuckDB 执行时报错的 SQL
MOCK_EXEC_ERROR_SQL = "SELECT nonexistent_func(amount) FROM t"

# 模拟被 markdown 代码块包裹的 SQL
MOCK_MARKDOWN_SQL = (
    "```sql\n"
    "SELECT sale_name, SUM(amount) AS total_amount FROM t GROUP BY sale_name\n"
    "```"
)

# 模拟中文列名 SQL：带双引号，应过护栏并执行成功
MOCK_CHINESE_SQL = (
    'SELECT "销售", SUM("销售额") AS total_amount FROM t '
    "WHERE \"状态\" = '已支付' GROUP BY \"销售\""
)


def _patch_llm(monkeypatch, response: str) -> None:
    """patch call_llm 返回固定 SQL 文本。"""
    monkeypatch.setattr(llm, "call_llm", lambda prompt: response)


def test_build_sql_prompt_constraints():
    """prompt 必须含硬约束、schema 表名列名、样例表头，且按行截断、总长 <8000。"""
    prompt = _build_sql_prompt(PARSED, SCHEMA, SAMPLE_CSV)

    assert "只允许使用表 t" in prompt
    assert "不许 JOIN" in prompt
    assert "字段名必须来自下面的 schema" in prompt
    assert "单条 SELECT" in prompt
    assert "中文注释" in prompt
    assert "如果对日期/时间字段做比较、分组或提取，必须显式进行类型转换" in prompt
    assert "CAST(created_at AS DATE)" in prompt
    assert "不要假设输入数据已经是 DATE 类型" in prompt

    assert "orders" in prompt
    for column in ["order_id", "sale_name", "amount", "status", "order_time"]:
        assert column in prompt
    assert "order_id,sale_name,amount,status" in prompt

    # 只取表头 + 前 3 行数据
    assert "王五" in prompt
    assert "赵六" not in prompt
    assert len(prompt) < 8000

    # 超长样例：丢弃整行并追加截断标记，不能出现半行
    long_prompt = _build_sql_prompt(PARSED, SCHEMA, SAMPLE_LONG_CSV)
    assert "…（样例已截断）" in long_prompt
    assert "9" * 600 in long_prompt
    assert "UNIQUE_MARKER" not in long_prompt
    assert len(long_prompt) < 8000


def test_generate_sql_success(monkeypatch):
    """正常 SQL：过护栏、过执行，success=True。"""
    _patch_llm(monkeypatch, MOCK_VALID_SQL)

    result = generate_sql(PARSED, SCHEMA, SAMPLE_CSV)

    assert result.success is True
    assert result.sql == MOCK_VALID_SQL
    assert result.requirement == "统计每个销售的成交额，排除退款"
    assert result.risks == []
    assert result.assumptions == ["金额单位为元"]
    assert result.questions == ["是否排除退款？"]


def test_generate_sql_guard_rejects_drop(monkeypatch):
    """含 DROP 的 SQL 被护栏拦截：success=False、risks 带 guard 原文、executor 未被调用。"""
    _patch_llm(monkeypatch, MOCK_DROP_SQL)

    called = {"count": 0}

    def fake_execute(sql, sample_csv):
        called["count"] += 1
        return None

    monkeypatch.setattr(sql_generator_module, "execute_sql", fake_execute)

    result = generate_sql(PARSED, SCHEMA, SAMPLE_CSV)

    assert result.success is False
    assert called["count"] == 0
    assert len(result.risks) == 1
    assert result.risks[0].startswith("SQL 未通过安全校验：")
    assert "[sql_guard]" in result.risks[0]
    assert "DROP" in result.risks[0]
    # 失败时模型对需求的理解仍保留
    assert result.assumptions == ["金额单位为元"]
    assert result.questions == ["是否排除退款？"]


def test_generate_sql_guard_rejects_unknown_column(monkeypatch):
    """引用不存在字段被护栏拦截：success=False，risks 含字段名。"""
    _patch_llm(monkeypatch, MOCK_UNKNOWN_COLUMN_SQL)

    result = generate_sql(PARSED, SCHEMA, SAMPLE_CSV)

    assert result.success is False
    assert result.risks[0].startswith("SQL 未通过安全校验：")
    assert "字段不存在" in result.risks[0]
    assert "nonexistent_column" in result.risks[0]


def test_generate_sql_executor_failure(monkeypatch):
    """过护栏但执行失败：success=False，risks 带 executor 原始错误。"""
    _patch_llm(monkeypatch, MOCK_EXEC_ERROR_SQL)

    result = generate_sql(PARSED, SCHEMA, SAMPLE_CSV)

    assert result.success is False
    assert result.risks[0].startswith("SQL 执行失败：")
    assert "nonexistent_func" in result.risks[0]
    assert result.sql == MOCK_EXEC_ERROR_SQL


def test_generate_sql_strips_markdown(monkeypatch):
    """markdown 包裹的 SQL 先剥离再走流程。"""
    _patch_llm(monkeypatch, MOCK_MARKDOWN_SQL)

    result = generate_sql(PARSED, SCHEMA, SAMPLE_CSV)

    assert result.success is True
    assert "```" not in result.sql
    assert result.sql.startswith("SELECT")


def test_generate_sql_chinese_columns(monkeypatch):
    """中文列名 SQL：过护栏、过执行，success=True 且结果列含中文原名。"""
    _patch_llm(monkeypatch, MOCK_CHINESE_SQL)

    captured = {}
    original_execute = sql_generator_module.execute_sql

    def wrapper(sql, sample_csv):
        execution = original_execute(sql, sample_csv)
        captured["execution"] = execution
        return execution

    monkeypatch.setattr(sql_generator_module, "execute_sql", wrapper)

    result = generate_sql(PARSED_CN, SCHEMA_CN, SAMPLE_CN)

    assert result.success is True
    assert captured["execution"].success is True
    assert "销售" in captured["execution"].columns
    assert captured["execution"].row_count == 1


def test_generate_sql_call_llm_error_raises(monkeypatch):
    """call_llm 抛异常时向上抛，不在 sql_generator 内捕获。"""

    def raise_error(prompt):
        raise RuntimeError("网络不可用")

    monkeypatch.setattr(llm, "call_llm", raise_error)

    with pytest.raises(RuntimeError):
        generate_sql(PARSED, SCHEMA, SAMPLE_CSV)
