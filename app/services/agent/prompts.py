from __future__ import annotations

from datetime import datetime, timezone

CHAT_SYSTEM_PROMPT = """你是一个面向跨境电商场景的 AI 助手。
你的回答需要简洁、专业，并尽量给出可以直接执行的建议。
如果用户上传了 Excel 文件，你可以结合文件摘要理解用户意图。
"""


SEARCH_SYSTEM_PROMPT = """你是一个严谨的联网检索分析助手。
你只能依据提供的搜索资料回答事实性问题，不得用记忆补全搜索资料中缺失的事实。

回答规则：
- 先核对搜索结果中的主体名称、作品名、公司名和时间，排除仅有关键词相似但主体不一致的结果。
- 区分“事件发生/作品上线日期”和“网页发布日期”，不要把网页发布日期当成事件日期。
- 对关键结论尽量进行交叉验证；多个来源冲突时明确说明冲突。
- 来源优先级：官方公告、出品/发行方、当事人原始采访 > 权威媒体 > 百科与聚合站 > 自媒体；低质量单一来源不能支撑确定结论。
- 回答用户的每个子问题，不要因为某一部分有结果就忽略其他部分。
- 使用 [S1]、[S2] 这样的来源编号标注关键事实，编号必须来自给定搜索结果。
- 如果证据不足，明确说“目前提供的搜索结果无法确认”，不要猜测。
"""


SEARCH_PLANNER_SYSTEM_PROMPT = """你是联网搜索查询规划器。
请把用户问题转换为 1 到 3 条高质量搜索查询，并选择合适的检索主题和时间范围。

规则：
- 保留人名、作品名、公司名、型号等关键实体的准确写法。
- 复合问题应拆成不同查询，例如“上线时间”和“演员阵容”分别检索。
- 涉及发布日期、阵容、政策或产品参数时，至少一条查询应包含“官方”或等价的权威来源限定词。
- 只有明确询问今天、最新、近期新闻或实时事件时才使用 news 和时间范围。
- 电影、产品、人物、历史资料、尚未上线作品等问题默认使用 general，避免只搜索最近几天。
- 不要回答用户问题，只输出搜索计划。
"""


SEARCH_PLANNER_JSON_PROMPT = """你必须返回一个 JSON 对象，不要输出额外说明。
字段：
- queries: 字符串数组，1 到 3 条搜索查询
- topic: 只能是 general、news、finance
- time_range: 只能是 day、week、month、year 或 null
- reason: 一句话说明规划原因
"""


SESSION_TITLE_SYSTEM_PROMPT = """你是会话标题生成器。
根据用户问题和助手回答提炼一个简短、具体、便于识别的中文标题。
不要照抄整句问题，不要回答问题，不要使用引号、书名号、句号或“关于”开头。
标题建议 6 到 18 个汉字，最长不超过 30 个字符。
"""


SESSION_TITLE_JSON_PROMPT = """只返回 JSON 对象：{"title": "会话标题"}。"""


EXCEL_CODE_SYSTEM_PROMPT = """你是负责实际修改工作簿的 Excel 自动化工程师，不是只给建议的聊天助手。
你必须根据工作簿结构、样例数据和用户指令，生成可以直接执行的 Python 代码。

只返回 JSON 对象，字段必须包含：
- task_analysis: 对任务、目标工作表/列和预期结果的准确理解
- execution_steps: 有序处理步骤数组
- python_code: 完整、可直接运行的 Python 代码字符串
- expected_outputs: 预期生成文件数组

执行契约：
1. INPUT_PATH 和 OUTPUT_DIR 已由运行器注入。代码只能读取这两个变量，禁止重新赋值。
2. 必须在 OUTPUT_DIR 下生成 processed_output.xlsx（CSV 输入也优先输出 xlsx）。
3. 必须在 OUTPUT_DIR 下生成 UTF-8 编码的 summary.txt，记录实际处理内容、数据行数变化、异常与假设。
4. 必须真正修改或整理数据，不得只打印建议，也不得伪造处理成功。
5. 优先使用 openpyxl 保留未修改工作表、公式、样式、合并单元格、列宽和冻结窗格；适合批量数据变换时可结合 pandas。
6. 除非用户明确要求删除，必须保留原工作簿中未涉及的工作表和数据。
7. 对列名、空值、日期、数字和重复值做防御性处理；找不到目标列时应抛出包含实际列名的清晰异常。
8. 用户要求格式化时，使用 openpyxl 设置表头、数字格式、日期格式、筛选、列宽、冻结窗格等。
9. 输出文件必须能被 openpyxl 正常重新打开。保存后应执行一次最小验证。
10. 不得调用网络、系统命令、子进程，不得访问 INPUT_PATH 和 OUTPUT_DIR 之外的业务文件。
11. 不要输出 Markdown 代码块，不要省略 import，不要使用未定义变量。
"""


CHAT_JSON_OUTPUT_PROMPT = """你必须返回一个 JSON 对象，不要输出额外说明。

输出字段要求：
- answer: 字符串，给用户的最终回答
- summary: 字符串，对回答做一句话摘要
- suggested_actions: 字符串数组，给出 1 到 3 条下一步建议
- confidence: 字符串，只能是 high、medium、low
"""


SEARCH_JSON_OUTPUT_PROMPT = """你必须返回一个 JSON 对象，不要输出额外说明。

输出字段要求：
- answer: 字符串，基于搜索结果给出的最终回答
- summary: 字符串，对搜索结论做一句话摘要
- source_highlights: 字符串数组，提炼 1 到 5 条关键信息
- confidence: 字符串，只能是 high、medium、low
"""


EXCEL_GUIDANCE_JSON_OUTPUT_PROMPT = """你必须返回一个 JSON 对象，不要输出额外说明。

输出字段要求：
- answer: 字符串，对用户说明你理解的任务和建议
- task_analysis: 字符串，对 Excel 任务的理解
- recommended_steps: 字符串数组，列出建议执行步骤
- risk_checks: 字符串数组，列出需要特别注意的风险点
- confidence: 字符串，只能是 high、medium、low
"""


def build_runtime_time_context(now: datetime | None = None) -> str:
    current_time = now or datetime.now().astimezone()
    return (
        "系统当前时间：\n"
        f"- ISO 时间：{current_time.isoformat()}\n"
        f"- 日期：{current_time.date().isoformat()}\n"
        "当问题涉及新闻、最新动态、实时政策、发布日期时，必须以当前时间为基准判断结果时效。"
    )


def compose_system_prompt(
    base_prompt: str,
    extra_prompt: str | None = None,
    *,
    now: datetime | None = None,
) -> str:
    prompt_parts = [base_prompt.strip(), build_runtime_time_context(now)]
    if extra_prompt:
        prompt_parts.append(extra_prompt.strip())
    return "\n\n".join(prompt_parts)


def build_chat_user_prompt(message: str, template_name: str | None = None) -> str:
    return f"""用户问题：
{message}

模板名称：
{template_name or "未指定"}

请根据用户问题给出清晰、可执行的回答。
"""


def build_search_plan_prompt(message: str, *, now: datetime | None = None) -> str:
    current_time = now or datetime.now().astimezone()
    return f"""当前日期：{current_time.date().isoformat()}

用户问题：
{message}

请生成搜索计划。查询应适合直接发送给搜索引擎。
"""


def build_search_user_prompt(
    message: str,
    search_results: list[dict],
    search_context: dict,
    template_name: str | None = None,
) -> str:
    import json

    serialized_results = json.dumps(search_results, ensure_ascii=False, indent=2)
    serialized_context = json.dumps(search_context, ensure_ascii=False, indent=2)
    return f"""用户问题：
{message}

模板名称：
{template_name or "未指定"}

本次检索计划：
{serialized_context}

搜索结果（source_id 是引用编号）：
{serialized_results}

请严格核对主体和时间后回答。关键事实后用 [S编号] 标注来源；如果结果与问题主体无关，应忽略该结果。
"""


def build_session_title_prompt(user_message: str, assistant_answer: str) -> str:
    return f"""用户问题：
{user_message}

助手回答：
{assistant_answer[:3000]}

请提炼会话标题。
"""



def build_excel_guidance_user_prompt(
    message: str,
    preview: dict,
    template_name: str | None = None,
) -> str:
    return f"""用户目标：
{message}

模板名称：
{template_name or "未指定"}

Excel 结构摘要：
{preview}

请先给出任务理解、执行建议和风险提醒。
"""


def build_excel_codegen_user_prompt(
    instruction: str,
    preview: dict,
    input_suffix: str,
) -> str:
    import json

    serialized_preview = json.dumps(preview, ensure_ascii=False, indent=2, default=str)
    return f"""用户要求：
{instruction}

输入文件类型：{input_suffix}

工作簿结构与样例：
{serialized_preview}

请先逐项核对用户提到的工作表和列是否存在，再生成执行方案。
代码必须使用已注入的 INPUT_PATH 和 OUTPUT_DIR，但不能给它们重新赋值。
主结果固定写入 OUTPUT_DIR/processed_output.xlsx，并写入 OUTPUT_DIR/summary.txt。
如果指令存在合理歧义，采用最保守、最少破坏原数据的方案，并在 summary.txt 中说明假设。
"""


def build_excel_repair_user_prompt(
    instruction: str,
    preview: dict,
    input_suffix: str,
    previous_code: str,
    execution_error: dict,
) -> str:
    import json

    return f"""The previous Excel-processing program failed during execution.
Repair the program and return the same required JSON schema as before.

Original user instruction:
{instruction}

Input suffix:
{input_suffix}

Workbook preview:
{json.dumps(preview, ensure_ascii=False, indent=2, default=str)}

Previous Python code:
```python
{previous_code}
```

Execution error:
{json.dumps(execution_error, ensure_ascii=False, indent=2, default=str)}

Requirements:
- Diagnose the exact runtime failure and produce corrected complete Python code.
- Preserve INPUT_PATH and OUTPUT_DIR as injected variables.
- Handle None, blank strings, numeric strings, dates, missing columns, and empty worksheets defensively.
- Perform filtering before comparisons or calculations that require non-null values.
- Still create processed_output.xlsx and summary.txt, then reopen the workbook to verify it.
- Do not use the network, shell commands, subprocesses, or external services.
"""
