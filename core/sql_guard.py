"""SQL 安全护栏模块。

职责：对生成或待执行的 SQL 做只读校验，并返回归一化后的 SQL。

约束：
- 只做校验，不执行 SQL，不调用 LLM；
- 返回值是规范化后的 SQL 字符串，不是布尔值；
- 失败一律抛出 ValueError，消息以 [sql_guard] 开头。
"""

# 文件结构：① _forbidden_label ② _validate_statements ③ _reject_forbidden_nodes
#           ④ _collect_names ⑤ _validate_tables ⑥ _validate_columns
#           ⑦ _quote_chinese_identifiers ⑧ ensure_read_only

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from core.models import SchemaInfo


# 根节点允许的类型：只读查询（UNION / INTERSECT / EXCEPT 是单一节点，必须放行）
ALLOWED_ROOTS = (
    exp.Select,
    exp.Union,
    exp.Intersect,
    exp.Except,
    exp.Subquery,
)

# 递归遍历时禁止出现的节点类型，覆盖 DDL / DML / 危险命令
_FORBIDDEN_NAMES = (
    "Insert",
    "Update",
    "Delete",
    "Drop",
    "Create",
    "Alter",
    "Pragma",
    "Attach",
    "Copy",
    "Install",
    "Load",
    "Command",
)

# 用 getattr 兼容不同 sqlglot 版本，缺失的节点类型自动跳过
FORBIDDEN_NODES = tuple(
    cls
    for cls in (getattr(exp, name, None) for name in _FORBIDDEN_NAMES)
    if cls is not None
)


def _forbidden_label(node: exp.Expression) -> str:
    """生成禁止节点的可读名称，用于错误消息。"""
    if isinstance(node, exp.Command):
        return str(node.this).upper()
    return type(node).__name__.upper()


def _validate_statements(statements: list[exp.Expression]) -> exp.Expression:
    """确认只有单条语句，且根节点是只读查询。"""
    if not statements or statements[0] is None:
        raise ValueError("[sql_guard] SQL 为空，无法校验")

    # parse 返回多条才是真多语句；UNION 属于单条语句
    if len(statements) != 1:
        raise ValueError(
            f"[sql_guard] 只允许单条查询语句，检测到 {len(statements)} 条语句"
        )

    root = statements[0]
    if not isinstance(root, ALLOWED_ROOTS):
        raise ValueError(f"[sql_guard] 只允许 SELECT，检测到 {_forbidden_label(root)}")
    return root


def _reject_forbidden_nodes(root: exp.Expression) -> None:
    """递归遍历整棵树，子查询里出现 DDL/DML 也要拒绝。"""
    for node in root.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise ValueError(f"[sql_guard] 只允许 SELECT，检测到 {_forbidden_label(node)}")


def _collect_names(root: exp.Expression) -> tuple[set[str], set[str], set[str], dict[str, str]]:
    """收集 CTE 名、子查询别名、投影别名与表别名映射。"""
    cte_names = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}
    derived_aliases = {
        sub.alias_or_name.lower()
        for sub in root.find_all(exp.Subquery)
        if sub.alias_or_name
    }
    projection_aliases = {
        alias.alias.lower() for alias in root.find_all(exp.Alias) if alias.alias
    }

    table_aliases: dict[str, str] = {}
    for table in root.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name:
            continue
        # 表名本身也可作为限定符使用
        table_aliases.setdefault(name, name)
        if table.alias:
            table_aliases[table.alias.lower()] = name

    return cte_names, derived_aliases, projection_aliases, table_aliases


def _validate_tables(
    root: exp.Expression,
    schema: SchemaInfo,
    cte_names: set[str],
    derived_aliases: set[str],
) -> None:
    """校验 exp.Table 节点，跳过 CTE 名与子查询别名。"""
    real_table = (schema.table_name or "").lower()
    for table in root.find_all(exp.Table):
        name = table.name or ""
        low = name.lower()
        if not low:
            continue
        if low in cte_names or low in derived_aliases:
            continue
        if not real_table or low != real_table:
            raise ValueError(f"[sql_guard] 表不存在：{name}")


def _validate_columns(
    root: exp.Expression,
    schema: SchemaInfo,
    cte_names: set[str],
    derived_aliases: set[str],
    projection_aliases: set[str],
    table_aliases: dict[str, str],
) -> None:
    """校验 exp.Column 节点，跳过 * 与 表名.*。"""
    schema_columns = {column.name.lower() for column in schema.columns}
    # 查询内部定义的别名（如 CTE 输出、投影别名）视为合法列引用，避免误报
    allowed_extra = cte_names | projection_aliases
    real_table = (schema.table_name or "").lower()

    for column in root.find_all(exp.Column):
        name = column.name or ""
        if not name or name == "*" or isinstance(column.this, exp.Star):
            continue

        qualifier = (column.table or "").lower()
        if qualifier:
            # 派生表的输出列无法用 schema 校验，直接跳过
            if qualifier in derived_aliases:
                continue
            resolved = table_aliases.get(qualifier, qualifier)
            if resolved not in cte_names and resolved != real_table:
                raise ValueError(f"[sql_guard] 表不存在：{column.table}")

        low = name.lower()
        if low in schema_columns or low in allowed_extra:
            continue
        display = f"{column.table}.{name}" if column.table else name
        raise ValueError(f"[sql_guard] 字段不存在：{display}")


def _quote_chinese_identifiers(root: exp.Expression, schema: SchemaInfo) -> None:
    """只给来自 schema 的中文列名 / 表名加双引号，字符串字面量不受影响。"""
    schema_names = {column.name for column in schema.columns}
    if schema.table_name:
        schema_names.add(schema.table_name)

    for identifier in root.find_all(exp.Identifier):
        name = identifier.name
        if not name or name.isascii():
            continue
        if name in schema_names:
            identifier.set("quoted", True)


def ensure_read_only(sql: str, schema: SchemaInfo) -> str:
    """校验 SQL 只读，返回归一化后的 SQL 字符串。

    校验流程：
    1. sqlglot 解析，必须恰好一条语句；
    2. 根节点只允许 SELECT / UNION / INTERSECT / EXCEPT / 子查询；
    3. 递归全树，出现 DDL / DML / PRAGMA / ATTACH / COPY / INSTALL / LOAD 即拒绝；
    4. 校验表名与列名是否存在于 schema；
    5. 给 schema 中的中文列名补双引号后输出。
    """
    if not sql or not sql.strip():
        raise ValueError("[sql_guard] SQL 为空，无法校验")

    try:
        statements = sqlglot.parse(sql, dialect="duckdb")
    except ParseError as exc:
        raise ValueError(f"[sql_guard] SQL 解析失败：{exc}") from exc

    root = _validate_statements(statements)
    _reject_forbidden_nodes(root)

    cte_names, derived_aliases, projection_aliases, table_aliases = _collect_names(root)
    _validate_tables(root, schema, cte_names, derived_aliases)
    _validate_columns(
        root,
        schema,
        cte_names,
        derived_aliases,
        projection_aliases,
        table_aliases,
    )

    _quote_chinese_identifiers(root, schema)
    return root.sql(dialect="duckdb")
