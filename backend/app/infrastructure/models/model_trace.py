from beanie import Document
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.domain.models.model_trace import ModelTraceRecord


class ModelTraceDocument(Document):
    trace_id: str
    user_id: str
    session_id: str
    record: ModelTraceRecord

    class Settings:
        name = "model_traces"
        indexes = [
            IndexModel([("trace_id", ASCENDING)], unique=True),
            IndexModel([("user_id", ASCENDING), ("session_id", ASCENDING), ("record.created_at", DESCENDING), ("trace_id", DESCENDING)]),
            IndexModel([("session_id", ASCENDING), ("record.task_id", ASCENDING)]),
        ]
