# Excel Agent

基于 FastAPI 的单用户本地 AI Agent，核心能力是：**上传 Excel/CSV，用自然语言描述清洗、整理、计算和格式化要求，系统实际执行代码并返回新的可下载文件。**

项目同时支持多轮对话、联网搜索、Excel 连续修改、模型生成会话标题，以及任务状态与执行元数据持久化。

## Excel 处理链路

1. 上传 `.xlsx`、`.xlsm` 或 `.csv`。
2. 解析工作表、列名、数据类型和样例数据。
3. DeepSeek 根据用户指令生成结构化执行计划和 Python 代码。
4. AST 安全校验生成代码，禁止覆盖 `INPUT_PATH` / `OUTPUT_DIR`，并拦截网络、系统命令和子进程等危险调用。
5. 优先在 E2B 隔离沙箱中执行；没有配置 E2B Key 时可回退到本地子进程执行器。
6. 若代码运行失败，系统会把脱敏后的 stderr/错误上下文交给模型自动修复并重试，默认最多修复 2 次。
7. 收集并校验所有产物，重新打开主工作簿确认文件有效。
8. 注册输出文件，返回 `output_file_id`、`outputs` 和下载地址。

默认主产物优先级：

1. `processed_output.xlsx`
2. `processed_output.xlsm`
3. `processed_output.csv`
4. 其他 Excel/CSV 文件

## 多轮对话与会话记忆

- 使用 `POST /api/chat/sessions` 显式创建新对话；客户端后续持续传递返回的 `session_id`。
- Redis 使用 `session:context:v2:{session_id}` 缓存完整上下文文档，默认 TTL 为 24 小时；读取不会刷新 TTL。
- Redis 缺失、过期、损坏或不可用时，从 MySQL 恢复成功回合和滚动摘要，并重新回填 Redis。
- 模型上下文由“系统提示词 + 较早滚动摘要 + 最近完整回合 + 当前消息”组成；默认最多 20 条历史消息、24,000 字符，并且不会拆散 user/assistant 回合。
- 未压缩成功回合达到第 21 轮时，最早 10 轮会合并到滚动摘要，最近 11 轮保留原文。MySQL 中的原始回合不会删除。
- 同一 `session_id` 的同步、流式聊天和 Excel 请求使用会话锁；并发请求返回 HTTP 409 和 `session_busy`。
- Excel 请求可省略 `file_id`，此时按最近成功 Excel 任务的输出文件、输入文件顺序继承；显式 `file_id` 始终优先。

## API

### 上传文件

`POST /api/files/upload`

使用 `multipart/form-data`，字段名为 `file`。

### 预览文件

`POST /api/files/preview`

```json
{
  "file_id": "file-xxx"
}
```

### 同步处理 Excel

`POST /api/files/process`

```json
{
  "file_id": "file-xxx",
  "instruction": "删除销售额为空的行，按销售额倒序，新增销售等级列，并把表头加粗、冻结首行",
  "session_id": "session-demo"
}
```

成功响应会包含：

- `task_id`
- `output_file_id`
- `outputs[]`
- `outputs[].download_url`
- `execution.mode`
- `execution.attempt_count`
- 输出工作簿预览

### 流式处理 Excel

`POST /api/files/process/stream`

请求体与同步接口相同，返回 SSE 状态、计划、代码、执行结果和完成事件。

### 通过聊天处理 Excel

先上传文件，再调用：

- `POST /api/chat/complete`
- `POST /api/chat/stream`

请求示例：

```json
{
  "message": "把空行删除，统一日期格式，按金额从高到低排序并返回新文件",
  "file_id": "file-xxx",
  "execution_mode": "e2b"
}
```

只要带有有效 `file_id`，Excel 意图会进入实际执行链路，不再只返回文字建议。

### 下载产物

`GET /api/files/{file_id}/download`

其中 `file_id` 使用处理响应中的 `output_file_id` 或 `outputs[].file_id`。

### 会话管理

创建新对话：

`POST /api/chat/sessions`

```json
{
  "title": null
}
```

查询会话和完整历史回合：

- `GET /api/chat/sessions?offset=0&limit=20`
- `GET /api/chat/sessions/{session_id}/turns?offset=0&limit=20`

聊天接口仍同时支持请求体 `session_id` 与请求头 `X-Session-ID`，请求体优先。再次调用创建接口并切换到新的 `session_id` 即为“开启新对话”。

### 其他接口

- `GET /health`：健康检查
- `POST /api/chat/complete`：非流式聊天/搜索/Excel 执行
- `POST /api/chat/stream`：SSE 流式聊天/搜索/Excel 执行
- `GET /api/tasks`：任务列表
- `GET /api/tasks/{task_id}`：任务详情与执行元数据

## 环境变量

在项目根目录的 `.env` 中配置：

```env
APP_NAME=Excel Agent
APP_ENV=development
DEBUG=true

MYSQL_DSN=mysql://user:password@127.0.0.1:3306/excel_agent
REDIS_URL=redis://127.0.0.1:6379/0

DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEFAULT_MODEL=deepseek-chat

EXECUTION_MODE=local
E2B_API_KEY=
E2B_EXEC_TIMEOUT_SECONDS=120
EXCEL_CODE_REPAIR_ATTEMPTS=2

SEARCH_PROVIDER=http
TAVILY_API_KEY=

REDIS_SESSION_MEMORY_TTL_SECONDS=86400
REDIS_LOCK_TTL_SECONDS=120
CONVERSATION_CONTEXT_MAX_MESSAGES=20
CONVERSATION_CONTEXT_MAX_CHARS=24000
CONVERSATION_SUMMARY_TRIGGER_TURNS=20
CONVERSATION_SUMMARY_BATCH_TURNS=10
CONVERSATION_SUMMARY_MAX_CHARS=4000
```

说明：

- 配置了 `E2B_API_KEY` 后，Excel 请求未显式指定 `execution_mode` 时默认优先使用 E2B。
- `EXECUTION_MODE=local` 只决定无 E2B Key 时的回退模式。
- 本地执行器已使用独立子进程和超时控制，但隔离能力仍弱于 E2B，生产环境建议使用 E2B。
- E2B 远程工作目录使用 `/tmp/excel-agent`，兼容官方代码解释器模板的写权限。
- MySQL 是会话历史与滚动摘要的权威数据源，Redis 是可过期的性能缓存；两者都未配置时，仅支持单进程内的临时多轮上下文。
- 上线前执行最新 Aerich 迁移，为 `chat_sessions` 增加摘要字段，并为会话和任务查询增加索引。

## 启动

```powershell
$env:UV_CACHE_DIR='D:\Agent\excel_agent\.uv-cache'
uv sync
uv run python -m app.main
```

启动后访问：

- API 文档：<http://127.0.0.1:8001/docs>
- 健康检查：<http://127.0.0.1:8001/health>

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q app migrations tests
```

测试覆盖会话回合存储、Redis/MySQL 上下文回退、20/21 轮摘要边界、完整回合滑动窗口、新会话隔离、Excel 文件继承、模型标题、联网搜索规划与去重、Excel 实际处理、代码安全校验以及执行失败后的自动修复重试。

## 数据库说明

当前任务表已经包含 `input_file_id`、`output_file_id` 和 `metadata_json`；多产物清单保存在任务元数据的 `excel.outputs` 中。对于当前本地单用户阶段，无需额外增加任务产物关联表。

数据库迁移使用 Tortoise ORM + Aerich：

```powershell
uv run aerich migrate --name "change_description"
uv run aerich upgrade
```
