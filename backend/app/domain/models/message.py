from typing import List
from pydantic import BaseModel
from app.domain.models.file import FileInfo
from app.domain.models.dataset import MountedDataset

class Message(BaseModel):
    message: str = ""
    attachments: List[str] = []
    attachment_file_ids: List[str] = []
    attachment_file_infos: List[FileInfo] = []
    skills: List[str] = []
    mcp_servers: List[str] = []
    datasets: List[MountedDataset] = []
    mcp_access_all: bool = False
