"""core/pandas_generator.py 的单元测试。

全部用例 patch core.llm.call_llm 与 core.pandas_generator.execute_pandas，
不发起真实网络请求、不真正启动子进程。
"""

# 文件结构：① mock 数据区 ② prompt 构造 ③ 静态校验 ④ 生成与执行

import pytest

import core.pandas_generator as pandas_generator_module
from core import llm
from core.models import (
    ColumnInfo,
    ExecutionResult,
    ParsedRequirement,
    SchemaInfo,
)
from core.pandas_generator import (
    _build_pandas_prompt,
    _validate_pandas_content,
    generate_pandas,
)


SCHEMA = SchemaInfo(
    table_name="orders",
    columns=[
        ColumnInfo(name="order_id", dtype="TEXT"),
        ColumnInfo(name="amount", dtype="DOUBLE"),
        ColumnInfo(name="status", dtype="TEXT"),
    ],
)

PARSED = ParsedRequirement(
    metrics=["amount"],
    dimensions=["status"],
    filters=["status != 'refund'"],
    original_requirement="统计各状态成交额，排除退款",
)

SAMPLE_CSV = "order_id,amount,status\n1,100,paid\n2,200,refund\n3,300,paid\n"

# 合法脚本：环境变量读 CSV、JSON 输出、中文注释
MOCK_VALID_SCRIPT = '''import os
import json
import pandas as pd

CSV_PATH = os.environ["SAMPLE_CSV_PATH"]
df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")

# 过滤退款记录
paid = df[df["status"] != "refund"]
result = paid
print(json.dumps({
    "columns": [str(c) for c in result.columns],
    "rows": json.loads(result.to_json(orient="records"))
}))
'''

# 含 eval 的脚本
MOCK_EVAL_SCRIPT = '''import os
import json
import pandas as pd

CSV_PATH = os.environ["SAMPLE_CSV_PATH"]
df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
value = eval("1 + 1")
print(json.dumps({"columns": [], "rows": []}))
'''

# 试图写文件的脚本
MOCK_WRITE_SCRIPT = '''import os
import json
import pandas as pd

CSV_PATH = os.environ["SAMPLE_CSV_PATH"]
df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
open("out.txt", "w").write("x")
print(json.dumps({"columns": [], "rows": []}))
'''

# 缺少 SAMPLE_CSV_PATH 的脚本
MOCK_NO_CSV_SCRIPT = '''import os
import json
import pandas as pd

df = pd.read_csv("data.csv")
print(json.dumps({"columns": [], "rows": []}))
'''

# 缺少 JSON 输出的脚本
MOCK_NO_JSON_SCRIPT = '''import os
import pandas as pd

CSV_PATH = os.environ["SAMPLE_CSV_PATH"]
df = pd.read_csv(CSV_PATH)
print("done")
'''

# 语法错误的脚本
MOCK_SYNTAX_ERROR_SCRIPT = '''import os
import json
import pandas as pd

CSV_PATH = os.environ["SAMPLE_CSV_PATH"]
def broken(:
    pass
print(json.dumps({"columns": [], "rows": []}))
'''

# 被 markdown 代码块包裹的合法脚本
MOCK_MARKDOWN_SCRIPT = "```python\n" + MOCK_VALID_SCRIPT + "\n```"


def _patch_llm(monkeypatch, response: str) -> None:
    """patch call_llm 返回固定脚本。"""
    monkeypatch.setattr(llm, "call_llm", lambda prompt: response)


def test_generate_pandas_success(monkeypatch):
    """合法脚本：静态检查通过、执行成功，success=True。"""
    _patch_llm(monkeypatch, MOCK_VALID_SCRIPT)
    calls = {"count": 0}

    def fake_execute(script, sample_csv):
        calls["count"] += 1
        return ExecutionResult(success=True, columns=["status"], row_count=2)

    monkeypatch.setattr(pandas_generator_module, "execute_pandas", fake_execute)

    result = generate_pandas(PARSED, SCHEMA, SAMPLE_CSV)

    assert result.success is True
    assert result.pandas == MOCK_VALID_SCRIPT.strip()
    assert result.requirement == "统计各状态成交额，排除退款"
    assert result.risks == []
    assert calls["count"] == 1


def test_generate_pandas_blocks_eval(monkeypatch):
    """含 eval 的脚本被静态检查拦截，executor 未被调用。"""
    _patch_llm(monkeypatch, MOCK_EVAL_SCRIPT)
    calls = {"count": 0}

    def fake_execute(script, sample_csv):
        calls["count"] += 1
        return ExecutionResult(success=True)

    monkeypatch.setattr(pandas_generator_module, "execute_pandas", fake_execute)

    result = generate_pandas(PARSED, SCHEMA, SAMPLE_CSV)

    assert result.success is False
    assert calls["count"] == 0
    assert result.risks[0].startswith("Pandas 脚本未通过静态检查：")
    assert "eval(" in result.risks[0]


def test_generate_pandas_blocks_write(monkeypatch):
    """试图写文件的脚本被静态检查拦截。"""
    _patch_llm(monkeypatch, MOCK_WRITE_SCRIPT)

    result = generate_pandas(PARSED, SCHEMA, SAMPLE_CSV)

    assert result.success is False
    assert "写文件" in result.risks[0]


def test_generate_pandas_requires_sample_csv_path(monkeypatch):
    """缺少 SAMPLE_CSV_PATH 的脚本被静态检查拦截。"""
    _patch_llm(monkeypatch, MOCK_NO_CSV_SCRIPT)

    result = generate_pandas(PARSED, SCHEMA, SAMPLE_CSV)

    assert result.success is False
    assert "SAMPLE_CSV_PATH" in result.risks[0]


def test_generate_pandas_requires_json_output(monkeypatch):
    """缺少 JSON 输出的脚本被静态检查拦截。"""
    _patch_llm(monkeypatch, MOCK_NO_JSON_SCRIPT)

    result = generate_pandas(PARSED, SCHEMA, SAMPLE_CSV)

    assert result.success is False
    assert "JSON 输出" in result.risks[0]


def test_generate_pandas_blocks_syntax_error(monkeypatch):
    """语法错误的脚本在 ast.parse 阶段被拦截。"""
    _patch_llm(monkeypatch, MOCK_SYNTAX_ERROR_SCRIPT)

    result = generate_pandas(PARSED, SCHEMA, SAMPLE_CSV)

    assert result.success is False
    assert "语法错误" in result.risks[0]


def test_generate_pandas_strips_markdown(monkeypatch):
    """markdown 包裹的脚本先剥离再校验与执行。"""
    _patch_llm(monkeypatch, MOCK_MARKDOWN_SCRIPT)
    monkeypatch.setattr(
        pandas_generator_module,
        "execute_pandas",
        lambda script, sample_csv: ExecutionResult(success=True, columns=["status"]),
    )

    result = generate_pandas(PARSED, SCHEMA, SAMPLE_CSV)

    assert result.success is True
    assert "```" not in result.pandas


def test_generate_pandas_executor_failure(monkeypatch):
    """静态检查通过但执行失败：success=False，risks 带 executor 原文。"""
    _patch_llm(monkeypatch, MOCK_VALID_SCRIPT)
    monkeypatch.setattr(
        pandas_generator_module,
        "execute_pandas",
        lambda script, sample_csv: ExecutionResult(
            success=False, error="boom"
        ),
    )

    result = generate_pandas(PARSED, SCHEMA, SAMPLE_CSV)

    assert result.success is False
    assert "Pandas 脚本执行失败：boom" in result.risks[0]


def test_generate_pandas_chinese_columns(monkeypatch):
    """中文列名脚本：静态检查通过，executor 返回的 columns 含中文。"""
    _patch_llm(monkeypatch, MOCK_VALID_SCRIPT)
    captured = {}

    def fake_execute(script, sample_csv):
        execution = ExecutionResult(
            success=True, columns=["状态", "销售额"], row_count=2
        )
        captured["execution"] = execution
        return execution

    monkeypatch.setattr(pandas_generator_module, "execute_pandas", fake_execute)

    result = generate_pandas(PARSED, SCHEMA, SAMPLE_CSV)

    assert result.success is True
    assert "状态" in captured["execution"].columns


def test_generate_pandas_call_llm_error_raises(monkeypatch):
    """call_llm 抛异常时向上抛，不在 generate_pandas 内捕获。"""

    def raise_error(prompt):
        raise RuntimeError("网络不可用")

    monkeypatch.setattr(llm, "call_llm", raise_error)

    with pytest.raises(RuntimeError):
        generate_pandas(PARSED, SCHEMA, SAMPLE_CSV)


def test_validate_pandas_content_valid_and_invalid():
    """_validate_pandas_content 合法脚本返回 []，违规脚本返回全部违规项。"""
    assert _validate_pandas_content(MOCK_VALID_SCRIPT) == []

    violations = _validate_pandas_content(MOCK_WRITE_SCRIPT)
    assert any("写文件" in item for item in violations)

    multi = _validate_pandas_content(MOCK_SYNTAX_ERROR_SCRIPT)
    assert any("语法错误" in item for item in multi)


def test_build_pandas_prompt_constraints():
    """prompt 必须含环境变量读取、模板、禁止清单，且总长 <8000。"""
    prompt = _build_pandas_prompt(PARSED, SCHEMA, SAMPLE_CSV)

    assert 'os.environ["SAMPLE_CSV_PATH"]' in prompt
    assert "禁止用 __file__ 相对路径" in prompt
    assert "json.dumps" in prompt
    assert "utf-8-sig" in prompt
    assert "禁止 eval" in prompt
    assert "to_csv" in prompt
    assert "result = df" in prompt
    assert "中文注释" in prompt
    assert "order_id" in prompt
    assert len(prompt) < 8000
