from tortoise import fields
from tortoise.models import Model


class ChatSession(Model):
    """会话主表：一条记录代表一个完整的聊天会话。"""

    # 会话主表：一条记录代表一个完整的聊天会话。
    # 主要用于把同一轮或同一段连续对话归到一个 session 下面。
    id = fields.IntField(pk=True, description="数据库自增主键")

    session_id = fields.CharField(
        max_length=64,
        unique=True,
        index=True,
        description="给前后端和业务层使用的会话唯一标识，不直接暴露数据库自增 id",
    )

    title = fields.CharField(
        max_length=255,
        null=True,
        description="会话标题，可由用户指定或系统自动总结生成",
    )

    created_at = fields.DatetimeField(auto_now_add=True, description="会话创建时间")

    updated_at = fields.DatetimeField(auto_now=True, description="会话最后更新时间")

    class Meta:

        table = "chat_sessions"
        table_description = "会话主表：保存聊天会话本身，不保存具体每条消息内容"


class ChatMessage(Model):
    """对话回合表：一条记录保存某个任务下的完整消息序列。"""

    id = fields.IntField(pk=True, description="数据库自增主键")

    session_id = fields.CharField(
        max_length=64,
        index=True,
        description="逻辑关联 chat_sessions.session_id，表示回合所属会话",
    )

    task_id = fields.CharField(
        max_length=64,
        unique=True,
        description="逻辑关联 agent_tasks.task_id，一条任务只保存一个对话回合",
    )

    message_json = fields.JSONField(
        default=list,
        description="该回合的 OpenAI 消息数组，按执行顺序保存 user、assistant、system、tool 消息",
    )

    created_at = fields.DatetimeField(auto_now_add=True, description="回合创建时间")

    updated_at = fields.DatetimeField(auto_now=True, description="回合最后更新时间")

    class Meta:
        table = "chat_messages"
        table_description = "对话回合表：每个 task 一条记录，message_json 保存该回合完整消息序列"
