# zh-data-codegen

面向中文业务场景的「数据处理代码生成 + 自动测试」小工具：输入中文需求、单表结构和少量样例数据，输出可执行的 DuckDB SQL / Pandas 脚本，并自动生成、自动执行 pytest 测试。

## 当前状态

v0.1.0-mvp 已冻结。9 个模块完成，111 个测试全过。

## 安装

```bash
conda create -n zh-data-codegen python=3.12 -y
conda activate zh-data-codegen

# 核心四件套（SQL 校验 / 本地执行 / LLM SDK / pytest 超时）
pip install sqlglot duckdb openai pytest-timeout

# 完整依赖（含 streamlit / pandas / pytest / pydantic）
pip install -r requirements.txt
```

## 环境变量

| 变量 | 必需 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | 是 | OpenAI 兼容接口密钥 |
| `OPENAI_BASE_URL` | 否 | 默认 `https://api.deepseek.com/v1` |
| `MODEL_NAME` | 否 | 默认 `deepseek-chat` |

Windows 可用 `set OPENAI_API_KEY=sk-xxx`，Linux/macOS 用 `export OPENAI_API_KEY=sk-xxx`。

## 运行

```bash
streamlit run app.py
```

未配置 `OPENAI_API_KEY` 时页面仍可启动，会提示先配置；点击生成会返回「缺少必需的环境变量 OPENAI_API_KEY」，不会白屏。

## 三种生成形态

| 选项 | 实际行为 |
|---|---|
| SQL | 生成并校验 SQL，再生成自动测试 |
| Pandas | 只生成并校验 Pandas 脚本 |
| 两者都要 | SQL + 自动测试 + Pandas 脚本 |

## 示例案例

`examples/` 下每个案例包含 `requirement.txt` / `schema.txt` / `sample.csv` / `expected.md`：

| 案例 | 场景 | 数据类型 |
|---|---|---|
| demand_001 | 每月复购率（排除退款） | 单表，英文字段 |
| demand_002 | 每个销售每月成交额环比 | 单表，英文字段 |
| demand_003 | 按月按产品统计退款率 | 单表，中文字段 |
| demand_004 | 合并来源并去重 | 单表，中文字段（Pandas 场景） |
| demand_005 | 新老客划分（首次购买时间） | 单表，中文字段（窗口函数） |

## 历史记录表结构

数据库固定为 `storage/history.db`，启动时自动建表：

```sql
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    requirement TEXT NOT NULL,
    schema_text TEXT NOT NULL,
    sample_csv TEXT NOT NULL,
    sql_success INTEGER,
    sql_text TEXT,
    sql_risks TEXT,
    pandas_success INTEGER,
    pandas_text TEXT,
    pandas_risks TEXT,
    tests_status TEXT,
    tests_content TEXT
)
```

- `sql_success` / `pandas_success` 用 0/1 存布尔；
- `*_risks` 存 JSON 数组字符串；
- 只做 INSERT 与最近 10 条 SELECT，不提供 UPDATE / DELETE。

## 测试

```bash
pytest tests/ -v
```

## 手工冒烟记录

首次手工冒烟已完成（2026-10-25，未配置真实 Key，用 mock / 真实接口跑了一次需求）。

结果：
- 工具成功拦截了模型生成的错误 SQL：DuckDB 返回 **Binder Error**——日期字段是 `VARCHAR` 未显式转成 `DATE`，且 `GROUP BY` 逻辑有误。
- 页面正确展示了红色报错框、具体失败原因，以及模型对需求理解正确的假设。
- 本次运行的历史记录成功写入 `storage/history.db`。

**结论：工具成功拦截了 AI 生成的错误 SQL，证明了护栏（sql_guard）与自动测试机制的价值。**
错误产物没有直接落到用户手里，而是在校验 / 执行阶段被拦截并给出可读原因。

真实 Key 的完整链路验证仍待配置 `OPENAI_API_KEY` 后补做。

## 边界

- 只做数据统计 / 清洗 / 报表类需求。
- 只读：禁止 DDL 与 DML。
- 不连接真实数据库，只接受用户粘贴的表结构与样例数据。
- MVP 只对接一个 OpenAI 兼容接口，不做多模型与多 Agent 编排。

## 已知限制

- MVP 仅支持单表分析，多表 JOIN 留待 v2。
- Pandas 执行只做基础防护（危险字符串拦截 + 超时 + 临时目录隔离），不是安全沙箱，不能防御刻意构造的恶意代码。
- 在部分受限沙箱环境中，mkdtemp 默认权限目录不可写，executor 会自动 fallback 到 os.makedirs 路径，生产环境不受影响；该环境也可能留下不可删的空目录，正常 Windows/Linux 不出现。
- 中文列名在 DuckDB SQL 中必须加双引号；sql_guard 会对 schema 中的中文列名自动补引号，直接调用 executor 时需自行保证。

## v2 待办

- 多表 JOIN 与多表 schema 解析。
- 规则化需求分类（自动判断 SQL / Pandas）。
- 历史记录详情页与删除能力。
- 更细的模型配置与超时策略。
- 前端组件拆分（`ui/` 目录）。

### 已完成（v0.1.1-mvp）

- ✅ 优化 prompt，强制要求 AI 在 SQL 中处理日期/时间字段时显式类型转换（如 `CAST AS DATE`）——首次手工冒烟暴露的高频问题，已修复。
