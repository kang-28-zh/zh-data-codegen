"""core/schema_parser.py 的单元测试。"""

import pytest

from core.schema_parser import parse_schema


def test_parse_single_create_table():
    """单段 CREATE TABLE 应正确解析表名、字段与类型，并跳过约束行。"""
    text = """
    CREATE TABLE orders (
        order_id TEXT,
        amount DECIMAL(10,2),
        status TEXT,
        PRIMARY KEY (order_id)
    )
    """

    schema = parse_schema(text)

    assert schema.table_name == "orders"
    assert [column.name for column in schema.columns] == ["order_id", "amount", "status"]
    assert schema.columns[1].dtype == "DECIMAL(10,2)"


def test_parse_natural_language_columns():
    """自然语言列清单应解析为默认表名 data。"""
    schema = parse_schema("order_id TEXT\namount DOUBLE\nstatus TEXT")

    assert schema.table_name == "data"
    assert [column.name for column in schema.columns] == ["order_id", "amount", "status"]


def test_parse_multiple_create_tables_raises():
    """两段 CREATE TABLE 必须报错，不静默取第一张表。"""
    text = "CREATE TABLE a (x INT); CREATE TABLE b (y INT);"

    with pytest.raises(ValueError) as exc_info:
        parse_schema(text)

    message = str(exc_info.value)
    assert "[schema_parser]" in message
    assert "仅支持单表" in message


def test_parse_multiple_header_blocks_raises():
    """多个「表名: 列...」表头块必须报错。"""
    text = "订单表: 订单号 TEXT，金额 DOUBLE\n客户表: 客户号 TEXT，名称 TEXT"

    with pytest.raises(ValueError) as exc_info:
        parse_schema(text)

    assert "仅支持单表" in str(exc_info.value)
