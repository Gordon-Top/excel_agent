from tortoise import fields
from tortoise.models import Model


class ChatSession(Model):
    """会话主表：一条记录代表一个完整的聊天会话。"""

    id = fields.IntField(pk=True, description="数据库自增主键")
    session_id = fields.CharField(max_length=64, unique=True, index=True)
    title = fields.CharField(max_length=255, null=True)
    summary = fields.TextField(null=True, description="较早成功回合的滚动摘要")
    summarized_turn_count = fields.IntField(default=0, description="已压缩的成功回合总数")
    summarized_through_turn_id = fields.IntField(null=True, description="最后一个已压缩回合的 chat_messages.id")
    summary_updated_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True, index=True)

    class Meta:
        table = "chat_sessions"
        table_description = "会话主表：保存会话、标题和滚动摘要"


class ChatMessage(Model):
    """对话回合表：一条记录保存某个任务下的完整消息序列。"""

    id = fields.IntField(pk=True)
    session_id = fields.CharField(max_length=64, index=True)
    task_id = fields.CharField(max_length=64, unique=True)
    message_json = fields.JSONField(default=list)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "chat_messages"
        table_description = "对话回合表：每个 task 一条记录，message_json 保存该回合完整消息序列"
