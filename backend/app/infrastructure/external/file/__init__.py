"""文件存储基础设施模块"""

from .factory import get_file_storage
from .gridfsfile import GridFSFileStorage

__all__ = ["GridFSFileStorage", "get_file_storage"]
