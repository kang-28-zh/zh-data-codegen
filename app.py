"""zh-data-codegen 的 Streamlit 单页入口。

页面只负责：读 session_state → 展示 → 按钮触发 _run_generation → 写 session_state。
生成逻辑全部在 _run_generation 内，异常统一捕获为结构化结果。
"""

import os

import streamlit as st

from core.pandas_generator import generate_pandas
from core.requirement_parser import parse_requirement
from core.schema_parser import parse_schema
from core.sql_generator import generate_sql
from core.storage import insert_run, read_recent_runs, risks_to_json
from core.test_generator import generate_tests


TARGET_OPTIONS = {"SQL": "sql", "Pandas": "pandas", "两者都要": "both"}

# 页面默认预填数据：方便评委/用户打开即可演示
DEFAULT_REQUIREMENT = (
    "统计每个月的复购率，排除退款订单。"
    "复购率 = 当月购买 2 次及以上的客户数 / 当月总购买客户数。"
)

DEFAULT_SCHEMA = """CREATE TABLE orders (
  order_id INT,
  customer_id INT,
  amount DECIMAL(10,2),
  status VARCHAR,
  created_at DATE
);"""

DEFAULT_SAMPLE_CSV = """order_id,customer_id,amount,status,created_at
1,101,100.00,paid,2024-01-05
2,102,200.00,paid,2024-01-10
3,101,150.00,paid,2024-01-15
4,103,300.00,refund,2024-01-20
5,104,50.00,paid,2024-01-25
6,101,120.00,paid,2024-02-05
7,102,200.00,paid,2024-02-10
8,105,80.00,paid,2024-02-15"""


def _apply_api_override(api_config: dict) -> dict:
    """临时把侧边栏配置写入环境变量，返回原值以便恢复。"""
    keys = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "MODEL_NAME")
    previous = {key: os.environ.get(key) for key in keys}
    os.environ["OPENAI_API_KEY"] = api_config["api_key"]
    os.environ["OPENAI_BASE_URL"] = (
        api_config.get("base_url") or "https://api.deepseek.com/v1"
    )
    os.environ["MODEL_NAME"] = api_config.get("model_name") or "deepseek-chat"
    return previous


def _restore_api_env(previous: dict) -> None:
    """恢复生成前的环境变量。"""
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _run_generation(inputs: dict, api_config: dict | None = None) -> dict:
    """执行完整生成流程，所有异常都转成结构化结果。

    如果传入 api_config 且包含 api_key，会在本次生成期间覆盖环境变量，
    生成结束后恢复原值；未传则完全走环境变量。
    """
    previous = None
    try:
        if api_config and api_config.get("api_key"):
            previous = _apply_api_override(api_config)

        schema = parse_schema(inputs["schema_text"])
        parsed = parse_requirement(inputs["requirement"], schema)

        sql_result = None
        test_result = None
        if inputs["target"] in ("sql", "both"):
            sql_result = generate_sql(parsed, schema, inputs["sample_csv"])
            if sql_result.success:
                test_result = generate_tests(
                    parsed, schema, sql_result.sql, inputs["sample_csv"]
                )

        pandas_result = None
        if inputs["target"] in ("pandas", "both"):
            pandas_result = generate_pandas(parsed, schema, inputs["sample_csv"])

        return {
            "success": True,
            "parsed": parsed,
            "schema": schema,
            "sql_result": sql_result,
            "test_result": test_result,
            "pandas_result": pandas_result,
        }
    except Exception as exc:  # 页面不能白屏，统一转结构化错误
        return {"success": False, "error": str(exc)}
    finally:
        if previous is not None:
            _restore_api_env(previous)


def _save_run(inputs: dict, result: dict) -> None:
    """把一次生成结果写入 SQLite 历史记录。"""
    sql_result = result.get("sql_result")
    test_result = result.get("test_result")
    pandas_result = result.get("pandas_result")

    record = {
        "requirement": inputs["requirement"],
        "schema_text": inputs["schema_text"],
        "sample_csv": inputs["sample_csv"],
        "sql_success": bool(sql_result and sql_result.success),
        "sql_text": sql_result.sql if sql_result else "",
        "sql_risks": risks_to_json(sql_result.risks) if sql_result else "[]",
        "pandas_success": bool(pandas_result and pandas_result.success),
        "pandas_text": pandas_result.pandas if pandas_result else "",
        "pandas_risks": risks_to_json(pandas_result.risks)
        if pandas_result
        else "[]",
        "tests_status": test_result.final_status if test_result else "",
        "tests_content": test_result.content if test_result else "",
    }
    insert_run(record)


def _render_result(result: dict) -> None:
    """展示一次生成结果，空串按「未生成」处理。"""
    if not result.get("success"):
        st.error(f"生成失败：{result.get('error', '未知错误')}")
        return

    parsed = result.get("parsed")
    sql_result = result.get("sql_result")
    test_result = result.get("test_result")
    pandas_result = result.get("pandas_result")

    if parsed is not None:
        st.subheader("需求理解")
        st.write(f"原始需求：{parsed.original_requirement}")
        st.write(
            f"指标：{parsed.metrics}｜维度：{parsed.dimensions}｜过滤：{parsed.filters}"
        )
        if parsed.questions:
            st.info("待确认：" + "；".join(parsed.questions))

    if sql_result is not None:
        st.subheader("SQL")
        if not sql_result.sql:
            st.info("未生成 SQL")
        else:
            st.code(sql_result.sql, language="sql")
        if not sql_result.success:
            st.error("SQL 未通过校验或执行失败")
            with st.expander("失败原因与建议"):
                for risk in sql_result.risks:
                    st.write(risk)
        if sql_result.assumptions:
            with st.expander("假设"):
                for item in sql_result.assumptions:
                    st.write(item)

    if test_result is not None:
        st.subheader("自动测试")
        if not test_result.content:
            st.info("未生成测试")
        else:
            st.code(test_result.content, language="python")
            st.write(
                f"状态：{test_result.final_status}｜轮数：{test_result.attempts}"
            )
        if test_result.final_status != "passed":
            st.error("测试未通过")
            with st.expander("失败原因与建议"):
                if test_result.failure_reason:
                    st.write(test_result.failure_reason)
                if test_result.suggestion:
                    st.write(test_result.suggestion)

    if pandas_result is not None:
        st.subheader("Pandas 脚本")
        if not pandas_result.pandas:
            st.info("未生成 Pandas 脚本")
        else:
            st.code(pandas_result.pandas, language="python")
        if not pandas_result.success:
            st.error("Pandas 脚本未通过校验或执行失败")
            with st.expander("失败原因与建议"):
                for risk in pandas_result.risks:
                    st.write(risk)


def _render_history() -> None:
    """展示最近 10 条历史记录。"""
    st.subheader("历史记录（最近 10 条）")
    try:
        runs = read_recent_runs(10)
    except Exception as exc:
        st.info(f"暂无历史记录：{exc}")
        return
    if not runs:
        st.info("暂无历史记录")
        return
    for run in runs:
        succeeded = bool(run.get("sql_success")) or bool(run.get("pandas_success"))
        status = "成功" if succeeded else "未成功"
        summary = str(run.get("requirement", ""))[:30]
        title = f"{run.get('created_at', '')}｜{summary}｜{status}"
        with st.expander(title):
            st.write(f"SQL 成功：{bool(run.get('sql_success'))}")
            st.write(f"Pandas 成功：{bool(run.get('pandas_success'))}")
            st.write(f"测试状态：{run.get('tests_status') or '未生成'}")


def main() -> None:
    """页面入口；只有 streamlit run 时才执行。"""
    st.set_page_config(
        page_title="一问三式 | 中文需求 -> 数据处理代码 + 自动测试", layout="wide"
    )
    st.title("一问三式 | 中文需求 -> 数据处理代码 + 自动测试")
    st.info(
        "💡 提示：输入框内为「复购率」的占位示例，"
        "直接点击生成，或输入您的真实需求覆盖它。"
    )

    with st.sidebar:
        st.header("API 配置")
        st.caption("这里填写的配置只对本次生成生效，留空则回退到环境变量。")
        api_key = st.text_input(
            "API Key",
            type="password",
            value=os.environ.get("OPENAI_API_KEY", ""),
        )
        base_url = st.text_input(
            "Base URL",
            value=os.environ.get("OPENAI_BASE_URL")
            or "https://api.deepseek.com/v1",
        )
        model_name = st.text_input(
            "Model Name",
            value=os.environ.get("MODEL_NAME") or "deepseek-chat",
        )

    requirement = st.text_area(
        "中文业务需求", placeholder=DEFAULT_REQUIREMENT, height=100
    )
    schema_text = st.text_area(
        "表结构（CREATE TABLE 或自然语言列清单）",
        placeholder=DEFAULT_SCHEMA,
        height=160,
    )
    sample_csv = st.text_area(
        "样例数据（CSV，5~20 行）", placeholder=DEFAULT_SAMPLE_CSV, height=160
    )
    target_label = st.radio(
        "生成形态", list(TARGET_OPTIONS.keys()), horizontal=True, index=0
    )

    if st.button("生成并验证", type="primary"):
        if not api_key and not os.environ.get("OPENAI_API_KEY"):
            st.error("请在左侧侧边栏配置您的 API Key 或配置环境变量")
        else:
            inputs = {
                "requirement": requirement,
                "schema_text": schema_text,
                "sample_csv": sample_csv,
                "target": TARGET_OPTIONS[target_label],
            }
            result = _run_generation(
                inputs,
                api_config={
                    "api_key": api_key,
                    "base_url": base_url,
                    "model_name": model_name,
                },
            )
            st.session_state["last_result"] = result
            try:
                _save_run(inputs, result)
            except Exception as exc:
                st.warning(f"历史记录写入失败：{exc}")

    result = st.session_state.get("last_result")
    if result:
        _render_result(result)

    st.divider()
    _render_history()


if __name__ == "__main__":
    main()
