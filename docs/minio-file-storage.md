# MinIO 文件存储

## 模式

`FILE_STORAGE_PROVIDER` 支持三种模式：

- `gridfs`：旧模式，文件继续写入 MongoDB GridFS。
- `minio`：只读写 MinIO。历史 GridFS 文件不会被自动识别。
- `hybrid`：推荐模式。新文件写入 MinIO，历史 GridFS ObjectId 文件继续可读。

## 文件 ID

MinIO 文件使用稳定文件 ID：

```text
minio:<uuid>
```

真实 MinIO object key 存在 MongoDB `stored_files` 集合中。不要把 object key 暴露为 `file_id`，因为 object key 包含 `/`，会破坏现有 `/files/{file_id}` 路由。

历史 GridFS 文件继续使用原 ObjectId。`hybrid` 模式也兼容显式 `gridfs:<object_id>`。

## 配置

```env
FILE_STORAGE_PROVIDER=hybrid
MINIO_ENDPOINT=minio:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=replace-with-a-strong-minio-password
MINIO_BUCKET=ai-dataseek-files
MINIO_SECURE=false
MINIO_PRESIGNED_EXPIRE_SECONDS=1800
MINIO_OBJECT_PREFIX=files
```

## 迁移

先 dry-run：

```bash
FILE_STORAGE_PROVIDER=hybrid uv run --project backend python scripts/migrate_gridfs_to_minio.py --dry-run
```

执行迁移：

```bash
FILE_STORAGE_PROVIDER=hybrid uv run --project backend python scripts/migrate_gridfs_to_minio.py
```

迁移脚本只复制文件并更新会话文件引用，不删除 GridFS 源文件。确认数据一致后再单独做 GridFS 清理。

## 回滚

如果 MinIO 不可用：

1. 将 `FILE_STORAGE_PROVIDER` 改回 `gridfs`。
2. 重启 backend。
3. 历史 GridFS 文件仍可访问；迁移后已改为 `minio:<uuid>` 的会话文件需要保留 MinIO 或恢复引用。

因此生产迁移建议先长时间保持 `hybrid`，确认稳定后再决定是否清理 GridFS。
