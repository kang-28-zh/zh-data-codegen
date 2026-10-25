"""core/sql_guard.py 的单元测试。

覆盖 11 类场景：正常 SELECT / DROP / 多语句 / 字段不存在 / 中文列名 /
子查询 / CTE / 窗口函数 / UNION ALL / CTE + UNION / 子查询别名。
"""

import pytest
import sqlglot

from core.models import ColumnInfo, SchemaInfo
from core.sql_guard import ensure_read_only


# 测试用 schema：同时包含英文与中文列名
SCHEMA = SchemaInfo(
    table_name="orders",
    columns=[
        ColumnInfo(name="order_id", dtype="TEXT"),
        ColumnInfo(name="customer_id", dtype="TEXT"),
        ColumnInfo(name="amount", dtype="DOUBLE"),
        ColumnInfo(name="status", dtype="TEXT"),
        ColumnInfo(name="order_time", dtype="TIMESTAMP"),
        ColumnInfo(name="订单号", dtype="TEXT"),
        ColumnInfo(name="金额", dtype="DOUBLE"),
    ],
)


def test_normal_select_passes():
    """正常单表 SELECT 应通过，并返回可再次解析的归一化 SQL。"""
    result = ensure_read_only(
        "SELECT order_id, amount FROM orders WHERE status = 'paid'", SCHEMA
    )

    assert isinstance(result, str)
    sqlglot.parse(result, dialect="duckdb")


def test_drop_rejected():
    """DROP 属于 DDL，必须拒绝，且消息包含语句类型。"""
    with pytest.raises(ValueError) as exc_info:
        ensure_read_only("DROP TABLE orders", SCHEMA)

    message = str(exc_info.value)
    assert message.startswith("[sql_guard]")
    assert "DROP" in message


def test_multi_statement_rejected():
    """多条语句必须拒绝。"""
    with pytest.raises(ValueError) as exc_info:
        ensure_read_only("SELECT 1; SELECT 2", SCHEMA)

    message = str(exc_info.value)
    assert message.startswith("[sql_guard]")
    assert "条语句" in message


def test_missing_column_rejected():
    """不存在的字段必须报错，并带出完整字段名。"""
    with pytest.raises(ValueError) as exc_info:
        ensure_read_only("SELECT orders.refund_time FROM orders", SCHEMA)

    message = str(exc_info.value)
    assert message.startswith("[sql_guard]")
    assert "refund_time" in message


def test_chinese_columns_quoted_and_reparse():
    """中文列名应自动加双引号，且结果仍能被 sqlglot 解析。"""
    result = ensure_read_only("SELECT 订单号, 金额 FROM orders", SCHEMA)

    assert '"订单号"' in result
    assert '"金额"' in result
    # 归一化结果必须还能再次解析，证明引号合法
    sqlglot.parse(result, dialect="duckdb")


def test_guard_quotes_chinese_alias():
    """中文别名也必须自动加双引号，包括不在 schema 里的别名。"""
    # 测试 SQL 用表 t，把 schema 表名对齐为 t
    schema_t = SCHEMA.model_copy(update={"table_name": "t"})

    result = ensure_read_only("SELECT amount AS 金额 FROM t", schema_t)
    assert 'AS "金额"' in result

    # 真实冒烟场景：别名不在 schema 里，也必须加引号
    renamed = ensure_read_only("SELECT order_time AS 月份 FROM t", schema_t)
    assert 'AS "月份"' in renamed


def test_subquery_allowed():
    """FROM 子查询应放行。"""
    sql = "SELECT * FROM (SELECT order_id, amount FROM orders WHERE amount > 0) AS t"
    result = ensure_read_only(sql, SCHEMA)

    sqlglot.parse(result, dialect="duckdb")


def test_cte_allowed():
    """CTE 应放行，CTE 名不能被当成真实表。"""
    sql = (
        "WITH monthly AS ("
        "SELECT order_time, SUM(amount) AS total FROM orders GROUP BY order_time"
        ") SELECT * FROM monthly"
    )
    result = ensure_read_only(sql, SCHEMA)

    sqlglot.parse(result, dialect="duckdb")


def test_window_function_allowed():
    """窗口函数应放行。"""
    sql = (
        "SELECT order_id, ROW_NUMBER() OVER ("
        "PARTITION BY customer_id ORDER BY amount DESC) AS rn FROM orders"
    )
    result = ensure_read_only(sql, SCHEMA)

    sqlglot.parse(result, dialect="duckdb")


def test_union_all_allowed():
    """UNION ALL 是只读查询，必须放行。"""
    sql = (
        "SELECT order_id FROM orders WHERE status = 'paid' "
        "UNION ALL "
        "SELECT order_id FROM orders WHERE status = 'refund'"
    )
    result = ensure_read_only(sql, SCHEMA)

    sqlglot.parse(result, dialect="duckdb")


def test_cte_with_union_allowed():
    """CTE 与 UNION 组合应放行。"""
    sql = (
        "WITH paid AS (SELECT order_id, amount FROM orders WHERE status = 'paid') "
        "SELECT order_id FROM paid "
        "UNION ALL "
        "SELECT order_id FROM orders WHERE status = 'refund'"
    )
    result = ensure_read_only(sql, SCHEMA)

    sqlglot.parse(result, dialect="duckdb")


def test_subquery_alias_not_treated_as_table():
    """子查询别名不能被误判为真实表。"""
    sql = "SELECT t.amount FROM (SELECT amount FROM orders) AS t"
    result = ensure_read_only(sql, SCHEMA)

    sqlglot.parse(result, dialect="duckdb")


def test_cte_output_alias_allowed():
    """CTE 输出的聚合别名应放行，不报字段不存在。"""
    sql = "WITH x AS (SELECT SUM(amount) AS total FROM orders) SELECT total FROM x"
    result = ensure_read_only(sql, SCHEMA)

    sqlglot.parse(result, dialect="duckdb")


def test_projection_alias_allowed():
    """投影 AS 别名应放行。"""
    result = ensure_read_only("SELECT SUM(amount) AS total_amount FROM orders", SCHEMA)

    sqlglot.parse(result, dialect="duckdb")


def test_alias_and_schema_column_mixed():
    """别名与真实字段混用时，status 走 schema 校验，cnt 走别名放行。"""
    sql = "SELECT status, COUNT(*) AS cnt FROM orders GROUP BY status"
    result = ensure_read_only(sql, SCHEMA)

    sqlglot.parse(result, dialect="duckdb")
