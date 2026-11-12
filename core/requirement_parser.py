"""中文需求解析模块。

职责：把「中文业务需求 + 表结构」解析成 ParsedRequirement 结构化结果。

约束：
- 只做解析，不做 SQL / Pandas 分类，不做字段翻译；
- 字段找不到时写进 questions，不报错；
- 只使用 json.loads 解析，禁止 eval / ast.literal_eval。
"""

# 文件结构：① _strip_code_fence（来自 core.utils）② _build_prompt ③ _build_retry_prompt
#           ④ _extract_tokens ⑤ _check_fields ⑥ parse_requirement

import difflib
import json

from core import llm
from core.models import ParsedRequirement, SchemaInfo
from core.utils import strip_code_fence as _strip_code_fence


# ParsedRequirement 的 7 个字段，必须全部出现在模型返回的 JSON 里
REQUIRED_FIELDS = (
    "metrics",
    "dimensions",
    "filters",
    "time_range",
    "order_by",
    "assumptions",
    "questions",
)

# prompt 中的字段说明模板；测试会逐条断言，防止随意改动
FIELD_TEMPLATES = (
    'metrics: 指标列表，如 ["成交额", "订单数"]，无则为 []',
    'dimensions: 维度列表，如 ["销售", "月份"]，无则为 []',
    'filters: 过滤条件列表，如 ["status=paid", "amount>100"]，无则为 []',
    'time_range: 时间范围，如 "2024-01 至 2024-12"，无则为 null',
    'order_by: 排序，如 "月份 ASC"，无则为 null',
    "assumptions: 你的假设列表，无则为 []",
    "questions: 需要向用户确认的问题列表，无则为 []",
)

# 重试 prompt 的截断上限
MAX_LAST_RETURN_CHARS = 2000
MAX_LAST_ERROR_CHARS = 500
MAX_RETRY_PROMPT_CHARS = 8000


def _build_prompt(requirement: str, schema: SchemaInfo) -> str:
    """构造解析需求的 prompt；不包含任何样例数据。"""
    columns = []
    for column in schema.columns:
        if column.dtype:
            columns.append(f"{column.name}({column.dtype})")
        else:
            columns.append(column.name)
    schema_line = f"{schema.table_name}: " + "，".join(columns)

    lines = [
        "你是数据分析助手，把中文业务需求解析成结构化 JSON，只输出 JSON。",
        "",
        "表结构：",
        schema_line,
        "",
        "字段说明：",
        *FIELD_TEMPLATES,
        "",
        "只输出 JSON，不要 markdown 代码块，不要任何解释文字。",
        "",
        f"业务需求：{requirement}",
    ]
    return "\n".join(lines)


def _build_retry_prompt(original: str, last_return: str, last_error: str) -> str:
    """构造重试 prompt，附加截断后的上次返回与错误信息。"""
    error_part = last_error[:MAX_LAST_ERROR_CHARS]
    return_part = last_return[:MAX_LAST_RETURN_CHARS]
    if len(last_return) > MAX_LAST_RETURN_CHARS:
        return_part += "…（已截断）"

    candidate = (
        f"{original}\n\n上一次返回内容：\n{return_part}"
        f"\n\n上一次解析错误：\n{error_part}\n\n请重新输出合法 JSON。"
    )
    if len(candidate) > MAX_RETRY_PROMPT_CHARS:
        # 超长时丢弃上次返回，只保留原 prompt 与错误信息
        candidate = (
            f"{original}\n\n上一次解析错误：\n{error_part}\n\n请重新输出合法 JSON。"
        )
    return candidate


def _extract_tokens(expression: str) -> list[str]:
    """提取表达式里的标识符 token，跳过单引号字符串字面量。"""
    tokens: list[str] = []
    current: list[str] = []
    in_string = False
    index = 0

    while index < len(expression):
        char = expression[index]
        if in_string:
            if char == "'":
                if index + 1 < len(expression) and expression[index + 1] == "'":
                    index += 2
                    continue
                in_string = False
            index += 1
            continue

        if char == "'":
            if current:
                tokens.append("".join(current))
                current = []
            in_string = True
            index += 1
            continue

        if char.isalnum() or char == "_":
            current.append(char)
        else:
            if current:
                tokens.append("".join(current))
                current = []
        index += 1

    if current:
        tokens.append("".join(current))
    return tokens


def _check_fields(parsed: ParsedRequirement, schema: SchemaInfo) -> ParsedRequirement:
    """检查 metrics / filters 引用的字段是否存在于 schema。

    找不到的字段从原列表移除，并在 questions 里给出提示；
    返回新对象，不修改入参。
    """
    result = parsed.model_copy(deep=True)

    known: dict[str, str] = {}
    for column in schema.columns:
        name = column.name.strip()
        if name:
            known[name.lower()] = name

    def resolve(expression: str) -> bool:
        # 逐个标识符比对，避免 refund_amount 被 amount 的子串误判
        return any(token.lower() in known for token in _extract_tokens(expression))

    for field_name in ("metrics", "filters"):
        kept: list[str] = []
        for expression in getattr(result, field_name):
            if resolve(expression):
                kept.append(expression)
                continue
            tokens = _extract_tokens(expression)
            hint = tokens[0] if tokens else expression.strip()
            matches = difflib.get_close_matches(
                hint.lower(), list(known.keys()), n=1, cutoff=0.6
            )
            if matches:
                result.questions.append(
                    f"未找到字段 {hint}，是否指 {known[matches[0]]}？"
                )
            else:
                result.questions.append(f"未找到字段 {hint}")
        setattr(result, field_name, kept)

    return result


def parse_requirement(requirement: str, schema: SchemaInfo) -> ParsedRequirement:
    """解析中文需求，返回结构化结果。

    调用模型最多 2 次：首次解析失败后，把截断后的上次返回与错误信息附加到 prompt 重试。
    """
    prompt = _build_prompt(requirement, schema)
    last_return = ""
    last_error = ""

    for attempt in range(2):
        current_prompt = (
            prompt
            if attempt == 0
            else _build_retry_prompt(prompt, last_return, last_error)
        )
        try:
            text = llm.call_llm(current_prompt)
        except Exception as exc:
            # OpenAI SDK 的超时异常类名是 APITimeoutError，不是内置 TimeoutError
            if type(exc).__name__ == "APITimeoutError" or isinstance(exc, TimeoutError):
                raise ValueError(
                    "[requirement_parser] 模型调用超时（60秒），请检查网络或稍后重试"
                ) from exc
            raise

        last_return = text
        try:
            payload = json.loads(_strip_code_fence(text))
            if not isinstance(payload, dict):
                raise ValueError("JSON 顶层不是对象")
            missing = [name for name in REQUIRED_FIELDS if name not in payload]
            if missing:
                raise ValueError("缺少字段：" + "、".join(missing))
            parsed = ParsedRequirement.model_validate(payload)
        except (ValueError, TypeError) as exc:
            last_error = str(exc)
            continue

        parsed.original_requirement = requirement
        return _check_fields(parsed, schema)

    raise ValueError(f"[requirement_parser] 模型两次返回非法 JSON：{last_error}")
