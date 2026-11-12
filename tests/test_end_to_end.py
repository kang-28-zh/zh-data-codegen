"""端到端测试：5 个示例案例全部 mock LLM，不发起真实网络请求。"""

# 文件结构：① 案例加载 ② mock LLM ③ 端到端流程 ④ _run_generation ⑤ app 导入

import json
import os

import pytest

import app
from core import llm
from core.pandas_generator import generate_pandas
from core.requirement_parser import parse_requirement
from core.schema_parser import parse_schema
from core.sql_generator import generate_sql
from core.test_generator import generate_tests


EXAMPLES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples"
)


def _read_example(name: str, filename: str) -> str:
    """读取 examples 下的案例文件。"""
    path = os.path.join(EXAMPLES_DIR, name, filename)
    with open(path, encoding="utf-8") as file:
        return file.read().strip()


# 5 个案例的 mock SQL（能过 guard 并能在各自样例数据上执行）
RAW_CASES = [
    (
        "demand_001",
        {
            "metrics": ["order_id"],
            "dimensions": ["order_time"],
            "filters": ["status != 'refund'"],
            "time_range": None,
            "order_by": None,
            "assumptions": [],
            "questions": [],
        },
        "SELECT customer_id, COUNT(DISTINCT order_id) AS order_count "
        "FROM t WHERE status != 'refund' GROUP BY customer_id",
    ),
    (
        "demand_002",
        {
            "metrics": ["amount"],
            "dimensions": ["sales_name"],
            "filters": ["status != 'refund'"],
            "time_range": None,
            "order_by": None,
            "assumptions": [],
            "questions": [],
        },
        "SELECT sales_name, substr(order_time, 1, 7) AS month, "
        "SUM(amount) AS total_amount FROM t WHERE status != 'refund' "
        "GROUP BY sales_name, substr(order_time, 1, 7)",
    ),
    (
        "demand_003",
        {
            "metrics": ["金额"],
            "dimensions": ["产品", "订单时间"],
            "filters": ["状态 != '退款'"],
            "time_range": None,
            "order_by": None,
            "assumptions": [],
            "questions": [],
        },
        'SELECT "产品", "状态", COUNT(*) AS order_count FROM t '
        'GROUP BY "产品", "状态"',
    ),
    (
        "demand_004",
        {
            "metrics": ["金额"],
            "dimensions": ["订单号", "客户名"],
            "filters": [],
            "time_range": None,
            "order_by": None,
            "assumptions": [],
            "questions": [],
        },
        'SELECT DISTINCT "订单号", "客户名", "金额" FROM t',
    ),
    (
        "demand_005",
        {
            "metrics": ["金额"],
            "dimensions": ["客户号"],
            "filters": [],
            "time_range": None,
            "order_by": None,
            "assumptions": [],
            "questions": [],
        },
        'SELECT "客户号", MIN("订单时间") AS first_order_time FROM t GROUP BY "客户号"',
    ),
]

# 案例标识符：必须是该案例 schema 里的唯一列名，_fake_call_llm 靠它分派
CASE_MARKERS = {
    "demand_001": "customer_id",
    "demand_002": "sales_name",
    "demand_003": "产品",
    "demand_004": "订单号",
    "demand_005": "客户号",
}

CASES = []
for name, parsed, sql in RAW_CASES:
    CASES.append(
        {
            "name": name,
            "parsed": parsed,
            "sql": sql,
            "marker": CASE_MARKERS[name],
            "requirement": _read_example(name, "requirement.txt"),
            "schema_text": _read_example(name, "schema.txt"),
            "sample_csv": _read_example(name, "sample.csv"),
        }
    )

# 防御：marker 必须只出现在一个案例的 schema 里，避免新增案例时分派错
for _marker in CASE_MARKERS.values():
    _hits = sum(1 for _case in CASES if _marker in _case["schema_text"])
    if _hits != 1:
        raise AssertionError(
            "案例标识符冲突，请更换 marker 或新增案例时更新 CASE_MARKERS"
        )


# 通用测试文件：对任意样例数据都能执行通过
GENERIC_TEST_FILE = '''import os
import pandas as pd
import duckdb

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample.csv")
SQL = """SELECT * FROM t"""


def test_reads_sample():
    """验证：样例数据可以被读取并执行 SQL。"""
    df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
    con = duckdb.connect(":memory:")
    con.register("t", df)
    result = con.execute(SQL).df()
    assert len(result) == len(df)
'''

# 通用 Pandas 脚本：原样输出样例数据
GENERIC_PANDAS_SCRIPT = '''import os
import json
import pandas as pd

CSV_PATH = os.environ["SAMPLE_CSV_PATH"]
df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")

# 原样输出样例数据
result = df
print(json.dumps({
    "columns": [str(c) for c in result.columns],
    "rows": json.loads(result.to_json(orient="records"))
}))
'''


def _fake_call_llm(prompt: str) -> str:
    """按 schema 中的唯一列名识别案例，再按 prompt 类型返回对应 mock。"""
    for case in CASES:
        if case["marker"] not in prompt:
            continue
        if "解析成结构化 JSON" in prompt:
            return json.dumps(case["parsed"], ensure_ascii=False)
        if "生成 DuckDB SQL" in prompt:
            return case["sql"]
        if "pytest 测试文件" in prompt:
            return GENERIC_TEST_FILE
        if "Pandas 脚本" in prompt:
            return GENERIC_PANDAS_SCRIPT
    raise AssertionError("未匹配到 prompt")


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_end_to_end_case(monkeypatch, case):
    """5 个案例跑完整链路：解析 → SQL → 测试 → Pandas。"""
    monkeypatch.setattr(llm, "call_llm", _fake_call_llm)

    schema = parse_schema(case["schema_text"])
    parsed = parse_requirement(case["requirement"], schema)

    sql_result = generate_sql(parsed, schema, case["sample_csv"])
    assert sql_result.success is True, sql_result.risks

    test_result = generate_tests(
        parsed, schema, sql_result.sql, case["sample_csv"]
    )
    assert test_result.final_status == "passed", test_result.failure_reason

    pandas_result = generate_pandas(parsed, schema, case["sample_csv"])
    assert pandas_result.success is True, pandas_result.risks


def test_run_generation_returns_dict(monkeypatch):
    """_run_generation 返回完整 dict，含所有 key。"""
    monkeypatch.setattr(llm, "call_llm", _fake_call_llm)
    case = CASES[0]

    result = app._run_generation(
        {
            "requirement": case["requirement"],
            "schema_text": case["schema_text"],
            "sample_csv": case["sample_csv"],
            "target": "sql",
        }
    )

    assert result["success"] is True
    for key in ("parsed", "schema", "sql_result", "test_result", "pandas_result"):
        assert key in result
    assert result["pandas_result"] is None


def test_run_generation_catches_exception(monkeypatch):
    """解析抛异常时应返回结构化错误，不裸抛。"""

    def boom(*args, **kwargs):
        raise RuntimeError("解析炸了")

    monkeypatch.setattr(app, "parse_requirement", boom)
    case = CASES[0]

    result = app._run_generation(
        {
            "requirement": case["requirement"],
            "schema_text": case["schema_text"],
            "sample_csv": case["sample_csv"],
            "target": "sql",
        }
    )

    assert result == {"success": False, "error": "解析炸了"}


def test_app_imports():
    """import app 不执行 UI，且 main 可调用。"""
    assert callable(app.main)
