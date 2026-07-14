from tortoise import fields
from tortoise.models import Model


class AgentTask(Model):
    """任务表：记录 Agent 每次实际执行动作。"""

    # Agent 任务主表：一条记录代表一次具体任务执行。
    # 例如一次聊天问答、一次联网搜索、一次 Excel 处理都可以记录成一个 task。
    id = fields.IntField(pk=True, description="数据库自增主键")

    task_id = fields.CharField(
        max_length=64,
        unique=True,
        index=True,
        description="给业务层使用的任务唯一标识，不直接暴露数据库自增 id",
    )

    session_id = fields.CharField(
        max_length=64,
        index=True,
        description="逻辑关联 所属会话 id，用于把任务和聊天上下文关联起来",
    )

    task_type = fields.CharField(
        max_length=32,
        index=True,
        description="任务类型，例如 chat、search、excel",
    )

    status = fields.CharField(
        max_length=32,
        index=True,
        description="任务状态，例如 pending、running、succeeded、failed",
    )

    instruction = fields.TextField(description="用户给这个任务的原始指令")

    execution_mode = fields.CharField(
        max_length=16,
        description="执行模式，例如 local 或 e2b",
    )

    template_name = fields.CharField(
        max_length=64,
        null=True,
        description="命中的模板名，例如月度订单统计、图片链接嵌入",
    )

    input_file_id = fields.CharField(
        max_length=64,
        null=True,
        description="逻辑关联 输入文件的逻辑 id，通常对应 stored_files.file_id",
    )

    output_file_id = fields.CharField(
        max_length=64,
        null=True,
        description="逻辑关联 输出文件的逻辑 id，通常对应处理后的结果文件",
    )

    result_summary = fields.TextField(
        null=True,
        description="任务执行完成后的摘要，适合展示在任务列表中",
    )

    error_message = fields.TextField(
        null=True,
        description="任务失败时记录错误信息，便于问题排查",
    )

    metadata_json = fields.JSONField(
        default=dict,
        description="任务执行元数据的唯一持久化来源，例如模型请求响应、搜索结果和引用",
    )

    created_at = fields.DatetimeField(auto_now_add=True, description="任务创建时间")

    updated_at = fields.DatetimeField(auto_now=True, description="任务更新时间")

    class Meta:

        table = "agent_tasks"
        table_description = "任务表：记录 Agent 每次实际执行动作，是任务历史和任务追踪的核心表"
        indexes = (("session_id", "status", "created_at"),)


class StoredFile(Model):
    """文件表：记录系统接收或生成的文件元数据。"""

    # 文件表：记录系统接收或生成的文件元数据。
    # 这里只存“文件信息”，不直接存文件二进制内容。
    id = fields.IntField(pk=True, description="数据库自增主键")

    file_id = fields.CharField(
        max_length=64,
        unique=True,
        index=True,
        description="给业务层和接口使用的文件唯一标识",
    )

    original_name = fields.CharField(
        max_length=255,
        description="文件原始名称，例如用户上传时看到的文件名",
    )

    stored_path = fields.TextField(description="文件在服务器上的实际存储路径")
    # 文件 MIME 类型，例如 application/vnd.openxmlformats-officedocument.spreadsheetml.sheet。
    mime_type = fields.CharField(
        max_length=100,
        description="文件 MIME 类型",
    )

    size_bytes = fields.BigIntField(description="文件大小，单位是字节")

    file_role = fields.CharField(
        max_length=16,
        description="文件角色，例如 input、output、temp",
    )

    created_at = fields.DatetimeField(auto_now_add=True, description="文件记录创建时间")

    class Meta:

        table = "stored_files"
        table_description = "文件表：用于追踪上传文件和生成结果文件，便于任务回溯和下载"
