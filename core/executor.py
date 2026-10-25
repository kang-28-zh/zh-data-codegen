"""沙箱执行模块。

职责：执行只读 SQL（DuckDB 内存库）、执行 Pandas 脚本、运行生成的 pytest 测试。

安全说明：
- Pandas 子进程只做基础防护（危险字符串拦截 + 超时 + 临时目录隔离），
  这是基础防护，不是安全沙箱，不能防御刻意构造的恶意代码；
- 不连接真实数据库，不访问网络。
"""

# 文件结构：① _make_temp_dir（临时目录兜底）② execute_sql ③ execute_pandas
#           ④ run_tests ⑤ _extract_failure_reason ⑥ _cleanup_temp_dir（清理重试）

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import duckdb
import pandas as pd

from core.models import ExecutionResult, TestResult


# 结果表最多展示的行数
MAX_ROWS = 20

# Pandas 脚本超时时间（秒）
PANDAS_TIMEOUT_SECONDS = 10

# pytest 执行超时时间（秒）
TESTS_TIMEOUT_SECONDS = 30

# 危险写法的基础拦截清单：这是基础防护，不是安全沙箱
_FORBIDDEN_PATTERN = re.compile(
    r"\b(eval|exec|__import__|os\.system|subprocess|socket|requests|urllib)\b"
)


def _make_temp_dir(prefix: str) -> str:
    """创建可写的临时目录。

    DSH 沙箱对 mkdtemp 默认 0o700 目录的 ACL 兼容兜底。
    正常 Windows/Linux 走 mkdtemp，fallback 只在沙箱环境触发。
    """
    path = tempfile.mkdtemp(prefix=prefix)
    probe = os.path.join(path, ".probe")
    try:
        with open(probe, "w", encoding="utf-8") as file:
            file.write("")
        os.remove(probe)
        return path
    except PermissionError:
        # mkdtemp 目录不可写时清理并改用 os.makedirs 创建
        shutil.rmtree(path, ignore_errors=True)
        fallback = os.path.join(tempfile.gettempdir(), f"{prefix}{uuid.uuid4().hex}")
        os.makedirs(fallback, exist_ok=True)
        return fallback


def _find_unquoted_non_ascii(sql: str) -> str | None:
    """找出第一个未加双引号、且不在字符串字面量/注释中的中文标识符。

    用于提示用户「中文列名需要加双引号」，不在本模块做自动修复。
    """
    index = 0
    length = len(sql)
    in_string = False
    in_identifier = False
    in_line_comment = False
    in_block_comment = False
    buffer: list[str] = []

    while index < length:
        char = sql[index]
        nxt = sql[index + 1] if index + 1 < length else ""

        if in_line_comment:
            if char == "\n":
                in_line_comment = False
            index += 1
            continue

        if in_block_comment:
            if char == "*" and nxt == "/":
                in_block_comment = False
                index += 2
                continue
            index += 1
            continue

        if in_string:
            if char == "'":
                # 处理 SQL 中的 '' 转义
                if nxt == "'":
                    index += 2
                    continue
                in_string = False
            index += 1
            continue

        if in_identifier:
            if char == '"':
                in_identifier = False
            index += 1
            continue

        if char == "-" and nxt == "-":
            in_line_comment = True
            index += 2
            continue

        if char == "/" and nxt == "*":
            in_block_comment = True
            index += 2
            continue

        if char == "'":
            in_string = True
            index += 1
            continue

        if char == '"':
            in_identifier = True
            index += 1
            continue

        if not char.isascii():
            buffer.append(char)
            index += 1
            continue

        if buffer:
            return "".join(buffer)
        index += 1

    if buffer:
        return "".join(buffer)
    return None


def execute_sql(sql: str, sample_csv: str) -> ExecutionResult:
    """在内存 DuckDB 中执行只读 SQL，失败不抛异常。

    - 样例数据用 pandas 读取后注册为临时表 t；
    - elapsed_ms 只统计 SQL 执行耗时，不含读取 CSV 的时间；
    - rows 最多保留 20 行，row_count 为真实总行数。
    """
    unquoted = _find_unquoted_non_ascii(sql)
    if unquoted:
        return ExecutionResult(
            success=False,
            error=f"[executor] 中文列名需要加双引号：{unquoted}",
        )

    try:
        df = pd.read_csv(io.StringIO(sample_csv), encoding="utf-8-sig")
    except Exception as exc:  # 样例数据本身不合法时也要返回结果，不抛异常
        return ExecutionResult(
            success=False,
            error=f"[executor] 样例数据加载失败：{exc}",
        )

    con = duckdb.connect(":memory:")
    started = None
    try:
        try:
            con.register("t", df)
            started = time.perf_counter()
            result_df = con.execute(sql).fetchdf()
        finally:
            con.close()
    except Exception as exc:
        elapsed = (
            int((time.perf_counter() - started) * 1000) if started is not None else 0
        )
        return ExecutionResult(
            success=False,
            error=f"[executor] SQL 执行失败：{exc}",
            elapsed_ms=elapsed,
        )

    elapsed = int((time.perf_counter() - started) * 1000)
    row_count = len(result_df)
    columns = [str(column) for column in result_df.columns]
    rows = json.loads(
        result_df.head(MAX_ROWS).to_json(orient="records", force_ascii=False)
    )

    return ExecutionResult(
        success=True,
        columns=columns,
        rows=rows,
        row_count=row_count,
        elapsed_ms=elapsed,
    )


def _prepare_pandas_env(csv_path: str) -> dict[str, str]:
    """基于当前环境变量复制，删除密钥，只补 SAMPLE_CSV_PATH。"""
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    env.pop("OPENAI_BASE_URL", None)
    env["SAMPLE_CSV_PATH"] = csv_path
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _read_text(path: str) -> str:
    """读取子进程输出文件，文件不存在或读取失败时返回空字符串。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as file:
            return file.read()
    except OSError:
        return ""


def _cleanup_temp_dir(path: str) -> None:
    """清理临时目录：最多重试 3 次，仍失败只打印提示，不影响返回结果。"""
    for _ in range(3):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        # Windows 下子进程刚退出时文件锁可能尚未释放，短暂等待后重试
        time.sleep(0.2)
    if os.path.exists(path):
        print(f"[executor] 临时目录清理失败：{path}")


def execute_pandas(script: str, sample_csv: str) -> ExecutionResult:
    """在子进程中执行 Pandas 脚本，失败不抛异常。

    脚本约定：从环境变量 SAMPLE_CSV_PATH 读取 CSV，把结果以 JSON 写到 stdout：
        {"columns": [...], "rows": [...]}
    """
    started = time.perf_counter()
    match = _FORBIDDEN_PATTERN.search(script)
    if match:
        return ExecutionResult(
            success=False,
            error=f"[executor] Pandas 脚本包含被禁止的操作：{match.group(0)}",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    tmpdir = _make_temp_dir("zh_codegen_pandas_")
    try:
        script_path = os.path.join(tmpdir, "script.py")
        csv_path = os.path.join(tmpdir, "sample.csv")
        with open(script_path, "w", encoding="utf-8") as file:
            file.write(script)
        with open(csv_path, "w", encoding="utf-8") as file:
            file.write(sample_csv)

        env = _prepare_pandas_env(csv_path)
        stdout_path = os.path.join(tmpdir, "stdout.txt")
        stderr_path = os.path.join(tmpdir, "stderr.txt")

        started = time.perf_counter()
        try:
            # 子进程输出重定向到文件：受限沙箱不允许创建管道（capture_output 会失败）
            with open(stdout_path, "w", encoding="utf-8") as stdout_file:
                with open(stderr_path, "w", encoding="utf-8") as stderr_file:
                    proc = subprocess.run(
                        [sys.executable, script_path],
                        cwd=tmpdir,
                        env=env,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        timeout=PANDAS_TIMEOUT_SECONDS,
                    )
        except subprocess.TimeoutExpired:
            elapsed = int((time.perf_counter() - started) * 1000)
            return ExecutionResult(
                success=False,
                error=f"[executor] Pandas 脚本执行超时（{PANDAS_TIMEOUT_SECONDS} 秒）",
                elapsed_ms=elapsed,
            )

        elapsed = int((time.perf_counter() - started) * 1000)

        stdout = _read_text(stdout_path).strip()
        stderr = _read_text(stderr_path).strip()

        if proc.returncode != 0:
            return ExecutionResult(
                success=False,
                error=f"[executor] Pandas 脚本执行失败：{stderr[:500]}",
                elapsed_ms=elapsed,
            )

        try:
            payload = json.loads(stdout)
            columns = [str(column) for column in payload["columns"]]
            all_rows = payload["rows"]
            if not isinstance(all_rows, list):
                raise TypeError("rows 不是列表")
        except (json.JSONDecodeError, KeyError, TypeError):
            return ExecutionResult(
                success=False,
                error=(
                    "[executor] Pandas 脚本输出非法 JSON："
                    f"stdout={stdout[:200]} stderr={stderr[:200]}"
                ),
                elapsed_ms=elapsed,
            )

        return ExecutionResult(
            success=True,
            columns=columns,
            rows=all_rows[:MAX_ROWS],
            row_count=len(all_rows),
            elapsed_ms=elapsed,
        )
    finally:
        _cleanup_temp_dir(tmpdir)


def _extract_failure_reason(output: str, test_name: str) -> str:
    """从 --tb=short 输出中提取某个用例的失败原因，截取前 500 字符。"""
    header = re.compile(r"^_+ " + re.escape(test_name) + r" _+$", re.MULTILINE)
    match = header.search(output)
    if match:
        rest = output[match.end():]
        end = re.search(r"\n_{5,}|short test summary info", rest)
        block = rest[: end.start()] if end else rest
        return block.strip()[:500]

    failed_line = re.compile(
        r"^FAILED \S*::" + re.escape(test_name) + r"\s*-\s*(.+)$",
        re.MULTILINE,
    )
    match = failed_line.search(output)
    if match:
        return match.group(1).strip()[:500]
    return ""


def run_tests(test_file_content: str, sample_csv: str) -> TestResult:
    """把测试内容写到临时目录并用 pytest 执行，失败不抛异常。"""
    tmpdir = _make_temp_dir("zh_codegen_tests_")
    try:
        test_path = os.path.join(tmpdir, "test_generated.py")
        csv_path = os.path.join(tmpdir, "sample.csv")
        with open(test_path, "w", encoding="utf-8") as file:
            file.write(test_file_content)
        with open(csv_path, "w", encoding="utf-8") as file:
            file.write(sample_csv)

        env = _prepare_pandas_env(csv_path)

        command = [
            sys.executable,
            "-m",
            "pytest",
            "test_generated.py",
            "--tb=short",
            "-v",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ]

        stdout_path = os.path.join(tmpdir, "pytest_stdout.txt")
        stderr_path = os.path.join(tmpdir, "pytest_stderr.txt")

        try:
            # 子进程输出重定向到文件：受限沙箱不允许创建管道（capture_output 会失败）
            with open(stdout_path, "w", encoding="utf-8") as stdout_file:
                with open(stderr_path, "w", encoding="utf-8") as stderr_file:
                    subprocess.run(
                        command,
                        cwd=tmpdir,
                        env=env,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        timeout=TESTS_TIMEOUT_SECONDS,
                    )
        except subprocess.TimeoutExpired:
            return TestResult(
                total=0,
                passed=0,
                failed=-1,
                details=[
                    {
                        "name": "<timeout>",
                        "status": "failed",
                        "reason": f"[executor] 测试执行超时（{TESTS_TIMEOUT_SECONDS} 秒）",
                    }
                ],
            )

        output = _read_text(stdout_path) + "\n" + _read_text(stderr_path)

        details: list[dict] = []
        for line in output.splitlines():
            match = re.search(r"::(\S+)\s+(PASSED|FAILED|ERROR)\b", line)
            if not match:
                continue
            name, status = match.group(1), match.group(2)
            if status == "PASSED":
                details.append({"name": name, "status": "passed", "reason": ""})
            else:
                details.append(
                    {
                        "name": name,
                        "status": "failed",
                        "reason": _extract_failure_reason(output, name),
                    }
                )

        if details:
            passed = sum(1 for item in details if item["status"] == "passed")
            failed = sum(1 for item in details if item["status"] == "failed")
        else:
            # 解析不到用例行时保持 details 为空，并用汇总行兜底计数
            passed_match = re.search(r"(\d+) passed", output)
            failed_match = re.search(r"(\d+) failed", output)
            passed = int(passed_match.group(1)) if passed_match else 0
            failed = int(failed_match.group(1)) if failed_match else 0
            # TestResult 没有 error 字段，原始输出打到日志便于排查
            print(f"[executor] pytest 原始输出（前 500 字符）：{output.strip()[:500]}")

        return TestResult(
            total=passed + failed,
            passed=passed,
            failed=failed,
            details=details,
        )
    finally:
        _cleanup_temp_dir(tmpdir)
