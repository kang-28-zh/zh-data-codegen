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


def _run_generation(inputs: dict) -> dict:
    """执行完整生成流程，所有异常都转成结构化结果。"""
    try:
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
        status = "成功" if run.get("sql_success") else "未成功"
        summary = str(run.get("requirement", ""))[:30]
        title = f"{run.get('created_at', '')}｜{summary}｜{status}"
        with st.expander(title):
            st.write(f"SQL 成功：{bool(run.get('sql_success'))}")
            st.write(f"Pandas 成功：{bool(run.get('pandas_success'))}")
            st.write(f"测试状态：{run.get('tests_status') or '未生成'}")


def main() -> None:
    """页面入口；只有 streamlit run 时才执行。"""
    st.set_page_config(page_title="zh-data-codegen", layout="wide")
    st.title("zh-data-codegen｜中文需求 → 数据处理代码 + 自动测试")

    if not os.environ.get("OPENAI_API_KEY"):
        st.warning("未检测到 OPENAI_API_KEY，生成会失败。请先配置环境变量后再试。")

    requirement = st.text_area("中文业务需求", height=100)
    schema_text = st.text_area("表结构（CREATE TABLE 或自然语言列清单）", height=160)
    sample_csv = st.text_area("样例数据（CSV，5~20 行）", height=160)
    target_label = st.radio(
        "生成形态", list(TARGET_OPTIONS.keys()), horizontal=True, index=0
    )

    if st.button("生成并验证", type="primary"):
        inputs = {
            "requirement": requirement,
            "schema_text": schema_text,
            "sample_csv": sample_csv,
            "target": TARGET_OPTIONS[target_label],
        }
        result = _run_generation(inputs)
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
