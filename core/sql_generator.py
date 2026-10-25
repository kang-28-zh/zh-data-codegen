"""SQL 生成模块。

职责：根据结构化需求与表结构，让模型生成 DuckDB SELECT，
并依次过安全护栏与沙箱执行，返回可用的 GenerationResult。

约束：
- 生成结果必须先过 sql_guard，再执行；护栏失败时不许调用 executor；
- 只有 call_llm 本身失败才允许抛异常，护栏/执行失败都返回结构化结果；
- 这是基础防护，不是安全沙箱。
"""

from core import llm
from core.executor import execute_sql
from core.models import GenerationResult, ParsedRequirement, SchemaInfo
from core.sql_guard import ensure_read_only
from core.utils import strip_code_fence


# 样例数据最多保留的行数（表头之外）
MAX_SAMPLE_ROWS = 3

# 样例数据块的最大字符数
MAX_SAMPLE_CHARS = 500


def _build_sql_prompt(
    parsed: ParsedRequirement,
    schema: SchemaInfo,
    sample_csv: str,
) -> str:
    """构造 SQL 生成 prompt。

    样例数据按行截断：表头 + 前 3 行，总量不超过 500 字符；
    截断逻辑内联在此处，不单独抽函数。
    """
    sample_lines = [
        line for line in (sample_csv or "").strip().splitlines() if line.strip()
    ]
    if sample_lines:
        kept = [sample_lines[0]]
        data_lines = sample_lines[1:]
        if data_lines:
            # 至少保留表头 + 第 1 行数据，即使这一行就超过 500 字符
            kept.append(data_lines[0])
        truncated = False
        for line in data_lines[1:MAX_SAMPLE_ROWS]:
            candidate = "\n".join(kept + [line])
            if len(candidate) > MAX_SAMPLE_CHARS:
                truncated = True
                break
            kept.append(line)
        sample_block = "\n".join(kept)
        if truncated:
            sample_block += "\n…（样例已截断）"
    else:
        sample_block = "（无样例数据）"

    columns = []
    for column in schema.columns:
        if column.dtype:
            columns.append(f"{column.name}({column.dtype})")
        else:
            columns.append(column.name)

    lines = [
        "你是数据分析助手，根据结构化需求和表结构生成 DuckDB SQL。",
        "",
        "硬性要求：",
        "1. 只允许使用表 t，不许 JOIN，不许写多表查询。",
        "2. 字段名必须来自下面的 schema，不许编造。",
        "3. 输出必须是单条 SELECT 语句，不加解释，不加 markdown 代码块。",
        "4. 用中文注释解释每个 CTE 或关键步骤。",
        "5. 如果对日期/时间字段做比较、分组或提取，必须显式进行类型转换"
        "（例如 CAST(created_at AS DATE) 或 strftime(CAST(created_at AS DATE), '%Y-%m')），"
        "不要假设输入数据已经是 DATE 类型。",
        "",
        f"表结构（SQL 中统一使用表 t 指代「{schema.table_name}」）：",
        "t: " + "，".join(columns),
        "",
        "样例数据（表 t）：",
        sample_block,
        "",
        "需求解析结果：",
        f"metrics: {parsed.metrics}",
        f"dimensions: {parsed.dimensions}",
        f"filters: {parsed.filters}",
        f"time_range: {parsed.time_range}",
        f"order_by: {parsed.order_by}",
        f"assumptions: {parsed.assumptions}",
        f"questions: {parsed.questions}",
    ]
    return "\n".join(lines)


def generate_sql(
    parsed: ParsedRequirement,
    schema: SchemaInfo,
    sample_csv: str,
) -> GenerationResult:
    """生成 SQL 并验证；失败时返回结构化结果，不抛异常（call_llm 异常除外）。"""
    prompt = _build_sql_prompt(parsed, schema, sample_csv)

    # call_llm 的网络/超时异常向上抛，不在本模块捕获
    raw_sql = llm.call_llm(prompt)
    sql = strip_code_fence(raw_sql)

    result = GenerationResult(
        success=False,
        requirement=parsed.original_requirement,
        sql=sql,
        assumptions=list(parsed.assumptions),
        questions=list(parsed.questions),
    )

    # SQL 统一使用表 t，护栏校验时把 schema 的表名对齐为 t
    guard_schema = schema.model_copy(update={"table_name": "t"})
    try:
        normalized_sql = ensure_read_only(sql, guard_schema)
    except ValueError as exc:
        result.risks.append(f"SQL 未通过安全校验：{exc}")
        return result

    try:
        execution = execute_sql(normalized_sql, sample_csv)
    except Exception as exc:  # executor 设计为不抛异常，这里兜底
        result.risks.append(f"SQL 执行失败：{exc}")
        return result

    if not execution.success:
        result.risks.append(f"SQL 执行失败：{execution.error}")
        return result

    result.success = True
    return result
