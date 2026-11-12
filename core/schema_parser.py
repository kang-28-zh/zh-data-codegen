"""表结构解析模块。

职责：把用户粘贴的表结构（CREATE TABLE 文本或自然语言列清单）解析为 SchemaInfo。

MVP 仅支持单表；检测到多张表时明确报错，不静默取第一张表。
"""

import re

from core.models import ColumnInfo, SchemaInfo

__all__ = ["ColumnInfo", "SchemaInfo", "parse_schema"]


# CREATE TABLE ... ( 的开头，括号配对由 _extract_create_tables 处理
_CREATE_TABLE_START = re.compile(
    r"CREATE\s+TABLE\s+([^\s(]+)\s*\(",
    re.IGNORECASE,
)

# CREATE TABLE 内的约束行，不是字段
_CONSTRAINT_PREFIXES = (
    "PRIMARY",
    "FOREIGN",
    "UNIQUE",
    "CONSTRAINT",
    "CHECK",
    "INDEX",
    "KEY",
)

# 字段类型后面的约束关键字，用于截断类型
_DTYPE_STOP_WORDS = (
    "NOT",
    "NULL",
    "PRIMARY",
    "UNIQUE",
    "DEFAULT",
    "REFERENCES",
    "CHECK",
    "COLLATE",
)

# 自然语言列清单的分隔符
_LIST_SEPARATORS = re.compile(r"[\n,，、;；]+")

# 「表名: 列...」形式的表头块
_HEADER_BLOCK = re.compile(r"^[^\s:：]+表\s*[:：]|^[^\s:：]+\s*[:：].*[,，]")


def _split_top_level(text: str) -> list[str]:
    """按顶层逗号切分，忽略括号内的逗号（如 DECIMAL(10,2)）。"""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current))
    return parts


def _extract_create_tables(text: str) -> list[tuple[str, str]]:
    """提取所有 CREATE TABLE 的 (表名, 定义体)，按括号配对扫描。"""
    results: list[tuple[str, str]] = []
    for match in _CREATE_TABLE_START.finditer(text):
        start = match.end()
        depth = 1
        index = start
        while index < len(text) and depth > 0:
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
            index += 1
        if depth != 0:
            raise ValueError("[schema_parser] CREATE TABLE 括号不匹配，无法解析")
        table_name = match.group(1).strip().strip('"`[]')
        results.append((table_name, text[start : index - 1]))
    return results


def _parse_column_definition(item: str) -> ColumnInfo | None:
    """解析单个字段定义；约束行返回 None。"""
    item = item.strip()
    if not item:
        return None

    tokens = item.split()
    if not tokens:
        return None
    if tokens[0].upper() in _CONSTRAINT_PREFIXES:
        return None

    name = tokens[0].strip('"`[]')
    dtype_tokens: list[str] = []
    for token in tokens[1:]:
        if token.upper() in _DTYPE_STOP_WORDS:
            break
        dtype_tokens.append(token)
    return ColumnInfo(name=name, dtype=" ".join(dtype_tokens))


def _parse_create_table(table_name: str, body: str) -> SchemaInfo:
    """解析单张 CREATE TABLE 的字段定义。"""
    columns = []
    for item in _split_top_level(body):
        column = _parse_column_definition(item)
        if column is not None:
            columns.append(column)

    if not columns:
        raise ValueError("[schema_parser] 未能从 CREATE TABLE 中解析出任何字段")

    return SchemaInfo(table_name=table_name, columns=columns)


def _parse_column_list(text: str) -> SchemaInfo:
    """解析自然语言列清单，默认表名为 data。"""
    columns = []
    for item in _LIST_SEPARATORS.split(text):
        column = _parse_column_definition(item)
        if column is not None:
            columns.append(column)

    if not columns:
        raise ValueError("[schema_parser] 未能解析出任何字段")

    return SchemaInfo(table_name="data", columns=columns)


def _count_table_headers(text: str) -> int:
    """统计「表名: 列...」形式的表头块数量。"""
    count = 0
    for line in text.splitlines():
        if _HEADER_BLOCK.match(line.strip()):
            count += 1
    return count


def parse_schema(schema_text: str) -> SchemaInfo:
    """解析表结构文本，返回 SchemaInfo。

    支持 CREATE TABLE 文本与自然语言列清单；MVP 仅支持单表。
    """
    text = (schema_text or "").strip()
    if not text:
        raise ValueError("[schema_parser] 表结构为空，无法解析")

    tables = _extract_create_tables(text)
    if len(tables) > 1:
        raise ValueError(
            f"[schema_parser] MVP 仅支持单表，检测到 {len(tables)} 张表，请合并为一张宽表"
        )
    if len(tables) == 1:
        table_name, body = tables[0]
        return _parse_create_table(table_name, body)

    header_count = _count_table_headers(text)
    if header_count > 1:
        raise ValueError(
            f"[schema_parser] MVP 仅支持单表，检测到 {header_count} 张表，请合并为一张宽表"
        )

    return _parse_column_list(text)
