"""测试自动生成模块（本项目差异化核心）。

职责：为已生成的 SQL 生成 pytest 测试文件，并在沙箱里验证；
最多重试 3 轮，失败时返回结构化的 TestGenerationResult。

约束：
- 生成阶段先做静态校验，违规不执行 run_tests；
- 只 patch call_llm 的测试不真调模型；
- suggestion 由代码模板生成，不调 LLM。
"""

# 文件结构：① 常量 ② _build_test_prompt ③ _validate_test_content
#           ④ _format_test_failure / _build_suggestion ⑤ generate_tests

import ast
import re

from core import llm
from core.executor import run_tests
from core.models import (
    ParsedRequirement,
    SchemaInfo,
    TestGenerationResult,
    TestResult,
)
from core.sql_guard import ensure_read_only
from core.utils import strip_code_fence


# prompt 截断上限
MAX_SQL_CHARS = 3000
MAX_LAST_CONTENT_CHARS = 2000
MAX_LAST_ERROR_CHARS = 500
MAX_PROMPT_CHARS = 8000

# 最多重试轮数
MAX_ATTEMPTS = 3

# 禁用断言的正则清单
FORBIDDEN_ASSERTION_PATTERNS = (
    re.compile(r"assert\s+\w+\s+is\s+not\s+None"),
    re.compile(r"assert\s+len\([^)]+\)\s*>\s*0"),
    re.compile(r"assert\s+True\b"),
    re.compile(r"assert\s+isinstance\([^)]+\)\s*$", re.MULTILINE),
)

# 禁止出现的导入与变量
FORBIDDEN_TOKENS = (
    "import core",
    "from core",
    "import models",
    "from models",
    "SAMPLE_CSV_PATH",
)


def _build_test_prompt(
    parsed: ParsedRequirement,
    schema: SchemaInfo,
    sql: str,
    last_content: str = "",
    last_error: str = "",
) -> str:
    """构造测试生成 prompt，含任务说明、模板、SQL、上一轮信息。"""
    sql_block = sql
    if len(sql_block) > MAX_SQL_CHARS:
        sql_block = (
            sql_block[:MAX_SQL_CHARS] + "\n…（SQL 已截断，请基于以下开头重新组织）"
        )

    content_block = ""
    if last_content:
        shown = last_content
        if len(shown) > MAX_LAST_CONTENT_CHARS:
            shown = shown[:MAX_LAST_CONTENT_CHARS] + "\n…（上次内容已截断）"
        content_block = "\n上一轮生成的测试文件：\n" + shown

    error_block = ""
    if last_error:
        shown_error = last_error
        if len(shown_error) > MAX_LAST_ERROR_CHARS:
            shown_error = shown_error[:MAX_LAST_ERROR_CHARS] + "\n…（错误已截断）"
        error_block = "\n上一轮失败原因：\n" + shown_error

    columns = "，".join(column.name for column in schema.columns)

    base_lines = [
        "你是数据测试工程师，请为下面的 SQL 生成 pytest 测试文件。",
        "只输出测试文件内容，不要解释。",
        "",
        "硬性规则：",
        "1. 只允许 import os、json、pandas、duckdb；禁止 import core、models、requests、socket、subprocess。",
        "2. 样例数据用 CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), \"sample.csv\") 读取，"
        "禁止使用 SAMPLE_CSV_PATH 环境变量。",
        "3. SQL 必须作为常量内嵌：SQL = \"\"\"...\"\"\"，不许读外部文件。",
        "4. 必须包含三类断言：结构断言（列名、行数）、业务不变量（如过滤退款后不含退款）、"
        "精确值断言（自造 3~5 行小数据，期望值手算可核对）。",
        "5. 禁止以下空断言：",
        "- assert result is not None",
        "- assert len(result) > 0",
        "- assert df.shape[0] > 0",
        "- assert isinstance(result, pd.DataFrame)",
        "- assert True",
        "6. 每个 def test_ 函数都必须有紧跟的中文 docstring，说明验证什么业务规则。",
        "7. 每个用例独立，不依赖执行顺序；fixture 不许写文件或访问网络。",
        "",
        "模板示例：",
        "```python",
        "import os",
        "import json",
        "import pandas as pd",
        "import duckdb",
        "",
        'CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample.csv")',
        'SQL = """<把下面的 SQL 原样放这里>"""',
        "",
        "def test_columns():",
        '    """验证：结果包含预期的列名。"""',
        "    df = pd.read_csv(CSV_PATH, encoding=\"utf-8-sig\")",
        "    con = duckdb.connect(\":memory:\")",
        "    con.register(\"t\", df)",
        "    result = con.execute(SQL).df()",
        "    assert set(result.columns) == {\"sale_name\", \"total_amount\"}",
        "",
        "def test_exclude_refund():",
        '    """验证：过滤退款后结果中不含退款订单。"""',
        "    data = pd.DataFrame({\"status\": [\"paid\", \"refund\"]})",
        "    paid = data[data[\"status\"] != \"refund\"]",
        "    assert \"refund\" not in paid[\"status\"].values",
        "",
        "def test_exact_value():",
        '    """验证：自造 3 行数据按规则汇总的结果等于手算值。"""',
        "    data = pd.DataFrame({\"sale_name\": [\"A\", \"A\", \"B\"], \"amount\": [100, 200, 300]})",
        "    assert data.groupby(\"sale_name\")[\"amount\"].sum()[\"A\"] == 300",
        "```",
        "",
        f"表结构（表 t 的原始表名为「{schema.table_name}」）：",
        f"列：{columns}",
        "",
        "需求解析结果：",
        f"需求：{parsed.original_requirement}",
        f"metrics: {parsed.metrics}",
        f"dimensions: {parsed.dimensions}",
        f"filters: {parsed.filters}",
        "",
        "待测试的 SQL：",
        sql_block,
    ]

    base = "\n".join(base_lines)
    prompt = base + content_block + error_block
    if len(prompt) > MAX_PROMPT_CHARS:
        # 超长时优先保留错误信息，丢弃上一轮测试文件
        prompt = base + error_block
    return prompt


def _has_docstring(node: ast.FunctionDef) -> bool:
    """判断函数体第一项是否是字符串 docstring。"""
    if not node.body:
        return False
    first = node.body[0]
    return (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    )


def _validate_test_content(content: str) -> list[str]:
    """静态检查生成的测试文件，返回违规项列表；空列表表示通过。"""
    violations: list[str] = []
    if not content.strip():
        return ["测试文件内容为空"]

    for pattern in FORBIDDEN_ASSERTION_PATTERNS:
        match = pattern.search(content)
        if match:
            violations.append(f"含禁用断言：{match.group(0).strip()}")

    for token in FORBIDDEN_TOKENS:
        if token in content:
            violations.append(f"禁止的导入或变量：{token}")

    if "SQL = " not in content:
        violations.append("缺少 SQL 常量（SQL = ...）")

    if "import pandas" not in content and "from pandas" not in content:
        violations.append("缺少 pandas 导入")
    if "import duckdb" not in content and "from duckdb" not in content:
        violations.append("缺少 duckdb 导入")

    try:
        tree = ast.parse(content)
    except SyntaxError as exc:
        violations.append(f"测试文件语法错误：{exc}")
        return violations

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            if not _has_docstring(node):
                violations.append(f"用例 {node.name} 缺 docstring")

    return violations


def _format_test_failure(result: TestResult) -> str:
    """把测试失败结果压成一句失败原因。"""
    failed = [item for item in result.details if item.get("status") == "failed"]
    if failed:
        first = failed[0]
        reason = str(first.get("reason", ""))[:200]
        return f"用例 {first.get('name', '')} 失败：{reason}"
    return f"测试未通过：passed={result.passed}, failed={result.failed}"


def _build_suggestion(kind: str, reason: str, result: TestResult | None) -> str:
    """按失败来源用代码模板生成修改建议，不调 LLM。"""
    if kind == "validate":
        return f"{reason}。建议模型重新生成时确保满足 prompt 中的约束。"

    if kind == "tests" and result is not None:
        failed = [item for item in result.details if item.get("status") == "failed"]
        if failed:
            first = failed[0]
            detail = str(first.get("reason", ""))[:200]
            return (
                f"用例 {first.get('name', '')} 失败：{detail}。"
                "建议检查对应 SQL 的 WHERE/GROUP BY 逻辑。"
            )
        return "测试未通过。建议检查对应 SQL 的 WHERE/GROUP BY 逻辑。"

    return f"{reason}。建议简化测试逻辑或增加超时时间。"


def generate_tests(
    parsed: ParsedRequirement,
    schema: SchemaInfo,
    generated_sql: str,
    sample_csv: str,
) -> TestGenerationResult:
    """生成并验证 pytest 测试文件，最多 3 轮。"""
    # 入参 SQL 先过护栏并归一化；失败不进入重试循环，也不调用 LLM
    guard_schema = schema.model_copy(update={"table_name": "t"})
    try:
        normalized_sql = ensure_read_only(generated_sql, guard_schema)
    except ValueError as exc:
        return TestGenerationResult(
            content="",
            attempts=0,
            final_status="failed",
            failure_reason=f"入参 SQL 未通过 guard：{exc}",
            suggestion="请先修复 SQL 生成环节",
            last_test_result=None,
        )

    last_content = ""
    last_result: TestResult | None = None
    last_reason = ""
    last_kind = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        prompt = _build_test_prompt(
            parsed,
            schema,
            normalized_sql,
            last_content=last_content if attempt > 1 else "",
            last_error=last_reason if attempt > 1 else "",
        )
        raw = llm.call_llm(prompt)
        content = strip_code_fence(raw)
        last_content = content

        violations = _validate_test_content(content)
        if violations:
            last_kind = "validate"
            last_reason = "静态检查未通过：" + "；".join(violations)
            continue

        try:
            test_result = run_tests(content, sample_csv)
        except Exception as exc:
            last_kind = "timeout"
            last_reason = f"测试执行异常：{type(exc).__name__}"
            last_result = None
            continue

        last_result = test_result

        if test_result.failed == -1:
            last_kind = "timeout"
            last_reason = "测试执行超时"
            continue

        if test_result.failed == 0 and test_result.total > 0:
            return TestGenerationResult(
                content=content,
                attempts=attempt,
                final_status="passed",
                failure_reason=None,
                suggestion=None,
                last_test_result=test_result,
            )

        last_kind = "tests"
        last_reason = _format_test_failure(test_result)

    return TestGenerationResult(
        content=last_content,
        attempts=MAX_ATTEMPTS,
        final_status="failed",
        failure_reason=last_reason[:500],
        suggestion=_build_suggestion(last_kind, last_reason, last_result),
        last_test_result=last_result,
    )
