"""数据模型定义。

职责：使用 pydantic 描述配置之外的跨模块数据结构，
所有跨模块传递的数据都必须使用本文件中的模型。

字段与 LLM 输出契约保持一致：

    requirement / sql / pandas / tests / assumptions / questions / risks
"""

from typing import Literal

from pydantic import BaseModel, Field


# 生成形态：sql / pandas / both，MVP 阶段由用户手动选择，默认 sql
GenerationTarget = Literal["sql", "pandas", "both"]


class GenerationRequest(BaseModel):
    """一次代码生成请求。"""

    requirement: str = ""
    """用户的中文业务需求描述。"""

    schema_text: str = ""
    """表结构原文，支持 CREATE TABLE 文本或自然语言列清单。"""

    sample_csv: str = ""
    """样例数据（CSV 文本），建议 5~20 行，超过 100 行提示截断。"""

    target: GenerationTarget = "sql"
    """生成形态，默认 sql。"""


class GenerationResult(BaseModel):
    """LLM 返回并按固定 JSON 字段解析后的生成结果。

    各生成器只填自己负责的字段，其余保持默认值：
    - sql_generator 填 success / requirement / sql / assumptions / questions / risks；
    - pandas_generator 填 success / requirement / pandas / assumptions / questions / risks。
    """

    success: bool = False
    """该生成器产物是否可用，不是「LLM 是否调用成功」。

    - LLM 调用失败（网络/超时）：抛异常，不返回本对象；
    - 产物不可用（guard 拦截 / 执行失败）：success=False，原因写进 risks；
    - 产物通过 guard 且执行成功：success=True。
    """

    requirement: str = ""
    """模型对需求的理解复述。"""

    sql: str = ""
    """生成的 DuckDB 方言 SELECT 语句。"""

    pandas: str = ""
    """生成的 Pandas 脚本。"""

    tests: str = ""
    """自动生成的 pytest 测试代码。"""

    assumptions: list[str] = Field(default_factory=list)
    """模型声明的前提假设。"""

    questions: list[str] = Field(default_factory=list)
    """模型需要用户补充确认的问题。"""

    risks: list[str] = Field(default_factory=list)
    """模型提示的风险点。"""


class ExecutionResult(BaseModel):
    """一次沙箱执行的结果，SQL 与 Pandas 共用同一结构。"""

    success: bool = False
    """是否执行成功。"""

    columns: list[str] = Field(default_factory=list)
    """结果表的列名。"""

    rows: list[dict] = Field(default_factory=list)
    """结果表数据，最多保留 20 行用于展示。"""

    row_count: int = 0
    """结果表总行数。"""

    elapsed_ms: int = 0
    """执行耗时，单位毫秒。"""

    error: str | None = None
    """失败原因；成功时为 None。"""


class TestResult(BaseModel):
    """一次 pytest 执行的结果。"""

    __test__ = False

    total: int = 0
    """用例总数。"""

    passed: int = 0
    """通过用例数。"""

    failed: int = 0
    """失败用例数。"""

    details: list[dict] = Field(default_factory=list)
    """每个用例的明细：用例名、状态、失败原因。"""


class TestGenerationResult(BaseModel):
    """测试自动生成的整体返回，成功与失败统一使用同一结构。"""

    __test__ = False

    content: str = ""
    """生成的 pytest 测试文件完整内容。"""

    attempts: int = 0
    """实际尝试轮数。"""

    final_status: Literal["passed", "failed"] = "failed"
    """最终状态：passed 或 failed。"""

    failure_reason: str | None = None
    """失败原因；成功时为 None。"""

    suggestion: str | None = None
    """给人工的修正建议；成功时为 None。"""

    last_test_result: TestResult | None = None
    """最后一次 pytest 执行结果。"""


class ColumnInfo(BaseModel):
    """单个字段的结构信息。"""

    name: str = ""
    """字段名，可能为中文。"""

    dtype: str = ""
    """字段类型；自然语言清单未给出时为空字符串。"""


class SchemaInfo(BaseModel):
    """一张表的解析结果。"""

    table_name: str = "data"
    """表名；未从输入中解析到表名时使用默认值 data。"""

    columns: list[ColumnInfo] = Field(default_factory=list)
    """字段列表。"""


class ParsedRequirement(BaseModel):
    """中文需求解析后的结构化结果。"""

    metrics: list[str] = Field(default_factory=list)
    """指标，例如销售额、复购率。"""

    dimensions: list[str] = Field(default_factory=list)
    """维度，例如月份、地区、品类。"""

    filters: list[str] = Field(default_factory=list)
    """过滤条件，例如排除退款。"""

    time_range: str | None = None
    """时间范围；未提及时为 None。"""

    order_by: str | None = None
    """排序要求；未提及时为 None。"""

    assumptions: list[str] = Field(default_factory=list)
    """解析时声明的前提假设。"""

    questions: list[str] = Field(default_factory=list)
    """需求存在歧义、需要用户确认的问题。"""

    original_requirement: str = ""
    """用户粘贴的原始需求文本，用于界面展示与回溯。"""
