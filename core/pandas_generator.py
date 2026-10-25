"""Pandas 脚本生成模块。

职责：根据结构化需求与表结构，让模型生成 Pandas 脚本，
静态检查后交给沙箱执行，返回可用的 GenerationResult。

约束：
- 没有 SQL 那样的归一化步骤，只有静态检查 + executor 基础防护；
- 只有 call_llm 本身失败才允许抛异常；
- 脚本必须通过环境变量 SAMPLE_CSV_PATH 读取样例数据。
"""

# 文件结构：① 常量 ② _build_pandas_prompt ③ _validate_pandas_content
#           ④ generate_pandas

import ast
import re

from core import llm
from core.executor import execute_pandas
from core.models import GenerationResult, ParsedRequirement, SchemaInfo
from core.utils import strip_code_fence


# 样例数据最多保留的数据行数（表头之外）
MAX_SAMPLE_ROWS = 3

# 样例数据块最大字符数
MAX_SAMPLE_CHARS = 500

# prompt 总长上限
MAX_PROMPT_CHARS = 8000

# 允许的 import 模块
ALLOWED_IMPORTS = {
    "os",
    "json",
    "pandas",
    "numpy",
    "datetime",
    "re",
    "math",
    "statistics",
}

# 危险调用黑名单（子串匹配）
DANGEROUS_TOKENS = (
    "eval(",
    "exec(",
    "os.system",
    "subprocess",
    "socket",
    "requests",
    "urllib",
    "__import__",
)

# 写文件黑名单
WRITE_OPEN_PATTERN = re.compile(r"open\([^)]*,\s*[\"'](?:w|a|wb|ab)[\"']")
WRITE_TOKENS = ("to_csv", "to_excel", "to_parquet")

# import 行匹配
IMPORT_PATTERN = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)", re.MULTILINE)


def _build_pandas_prompt(
    parsed: ParsedRequirement,
    schema: SchemaInfo,
    sample_csv: str,
) -> str:
    """构造 Pandas 脚本生成 prompt；模板不截断，超长时优先裁剪样例数据。"""
    sample_lines = [
        line for line in (sample_csv or "").strip().splitlines() if line.strip()
    ]

    if sample_lines:
        header = sample_lines[0]
        data_lines = sample_lines[1:]
        kept = [header]
        if data_lines:
            kept.append(data_lines[0])
        truncated = False
        for line in data_lines[1:MAX_SAMPLE_ROWS]:
            candidate = "\n".join(kept + [line])
            if len(candidate) > MAX_SAMPLE_CHARS:
                truncated = True
                break
            kept.append(line)
        full_sample = "\n".join(kept)
        if truncated:
            full_sample += "\n…（样例已截断）"
    else:
        header = ""
        data_lines = []
        full_sample = "（无样例数据）"

    columns = "，".join(column.name for column in schema.columns)

    base_lines = [
        "你是数据分析助手，请生成只读的 Pandas 脚本，用 JSON 把结果输出到 stdout。",
        "",
        "硬性规则：",
        '1. 读取样例数据必须用 os.environ["SAMPLE_CSV_PATH"]，禁止用 __file__ 相对路径。',
        '2. 只用 pd.read_csv(CSV_PATH, encoding="utf-8-sig") 读取，防 BOM。',
        "3. 只允许 import os、json、pandas、numpy、datetime、re、math、statistics。",
        "4. 禁止 eval、exec、__import__、os.system、subprocess、socket、requests、urllib。",
        '5. 禁止写文件：禁止 open(..., "w")、to_csv、to_excel、to_parquet。',
        '6. 结果必须用 print(json.dumps({...})) 输出，columns 必须转成 str。',
        "7. 必须有中文注释解释业务逻辑。",
        "8. 只输出脚本，不要解释，不要 markdown 代码块。",
        "",
        "模板：",
        "import os",
        "import json",
        "import pandas as pd",
        "",
        'CSV_PATH = os.environ["SAMPLE_CSV_PATH"]',
        'df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")',
        "",
        "# 业务逻辑，中文注释",
        "",
        "result = df  # 替换为实际结果",
        "print(json.dumps({",
        '    "columns": [str(c) for c in result.columns],',
        '    "rows": json.loads(result.to_json(orient="records"))',
        "}))",
        "",
        f"表结构（原始表名「{schema.table_name}」）：",
        f"列：{columns}",
        "",
        "需求解析结果：",
        f"需求：{parsed.original_requirement}",
        f"metrics: {parsed.metrics}",
        f"dimensions: {parsed.dimensions}",
        f"filters: {parsed.filters}",
    ]

    def make_prompt(sample_text: str) -> str:
        return "\n".join(base_lines + ["", "样例数据（表 t）：", sample_text])

    prompt = make_prompt(full_sample)
    if len(prompt) > MAX_PROMPT_CHARS:
        # 优先裁剪样例数据：保留表头 + 1 行
        compact = header + ("\n" + data_lines[0] if data_lines else "")
        prompt = make_prompt(compact)
    if len(prompt) > MAX_PROMPT_CHARS:
        # 再不济只保留表头
        prompt = make_prompt(header or "（无样例数据）")
    return prompt


def _validate_pandas_content(content: str) -> list[str]:
    """静态检查生成的 Pandas 脚本，返回违规项列表；空列表表示通过。"""
    violations: list[str] = []
    if not content.strip():
        return ["脚本内容为空"]

    for match in IMPORT_PATTERN.finditer(content):
        module = match.group(1).split(".")[0]
        if module not in ALLOWED_IMPORTS:
            violations.append(f"含未允许的 import：{module}")

    for token in DANGEROUS_TOKENS:
        if token in content:
            violations.append(f"含危险调用：{token}")

    if "SAMPLE_CSV_PATH" not in content:
        violations.append("缺少 SAMPLE_CSV_PATH 环境变量读取")

    if "json.dumps" not in content and "json.loads" not in content:
        violations.append("缺少 JSON 输出（json.dumps / json.loads）")

    if "print(" not in content:
        violations.append("缺少 print( 输出")

    open_match = WRITE_OPEN_PATTERN.search(content)
    if open_match:
        violations.append(f"脚本试图写文件：{open_match.group(0)}")
    for token in WRITE_TOKENS:
        if token in content:
            violations.append(f"脚本试图写文件：{token}")

    try:
        ast.parse(content)
    except SyntaxError as exc:
        violations.append(f"脚本语法错误：{exc}")

    return violations


def generate_pandas(
    parsed: ParsedRequirement,
    schema: SchemaInfo,
    sample_csv: str,
) -> GenerationResult:
    """生成 Pandas 脚本并静态/执行验证；失败返回结构化结果（call_llm 异常除外）。"""
    prompt = _build_pandas_prompt(parsed, schema, sample_csv)

    # call_llm 的网络/超时异常向上抛
    raw_script = llm.call_llm(prompt)
    script = strip_code_fence(raw_script)

    result = GenerationResult(
        success=False,
        requirement=parsed.original_requirement,
        pandas=script,
        assumptions=list(parsed.assumptions),
        questions=list(parsed.questions),
    )

    violations = _validate_pandas_content(script)
    if violations:
        result.risks.append(
            "Pandas 脚本未通过静态检查：" + "；".join(violations)
        )
        return result

    try:
        execution = execute_pandas(script, sample_csv)
    except Exception as exc:  # executor 设计为不抛异常，这里兜底
        result.risks.append(f"Pandas 脚本执行失败：{exc}")
        return result

    if not execution.success:
        result.risks.append(f"Pandas 脚本执行失败：{execution.error}")
        return result

    result.success = True
    return result
