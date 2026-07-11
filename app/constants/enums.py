from enum import StrEnum


class AgentIntent(StrEnum):
    CHAT = "chat"
    SEARCH = "search"
    EXCEL = "excel"


class TaskType(StrEnum):
    CHAT = "chat"
    SEARCH = "search"
    EXCEL = "excel"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ExecutionMode(StrEnum):
    LOCAL = "local"
    E2B = "e2b"


class FileRole(StrEnum):
    INPUT = "input"
    OUTPUT = "output"
    TEMP = "temp"


class SSEEventType(StrEnum):
    STATUS = "status"
    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    CODE = "code"
    RESULT = "result"
    ERROR = "error"
    DONE = "done"
