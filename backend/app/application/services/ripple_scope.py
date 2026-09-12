"""Exact .rpl/.raw dataset pairing; reuse established pair stat/final fences."""
from __future__ import annotations
import asyncio
import json
from pathlib import PurePosixPath
from app.application.services.dataset_file_preview import PREFIX
from app.application.services.envi_scope import EnviDatasetScope
from app.application.services.ripple_window_visualization import validate_ripple_window_options
from app.application.services.scientific_visualization import ScientificPreviewRejected
from app.application.services.window_visualization import window_visualization


class RippleDatasetScope(EnviDatasetScope):
    def _inventory(self, row, dataset):
        header=PurePosixPath(row["path"])
        if header.suffix.lower()!=".rpl":
            raise ScientificPreviewRejected("Ripple 请从已登记数据集的 .rpl 文件打开。")
        self.previews._declared(dataset,row["path"])
        self.previews._visible_path(dataset,row["path"])
        base=header.with_suffix("")
        matches=[item for item in dataset.files if PurePosixPath(item.path).suffix.lower()==".raw" and PurePosixPath(item.path).with_suffix("")==base]
        if len(matches)!=1:
            raise ScientificPreviewRejected("Ripple 需要同目录、同名且唯一的 .raw 配对文件，不跟随头文件路径。")
        entries,location=[],None
        for key,path in (("header",row["path"]),("data",matches[0].path)):
            declared=self.previews._declared(dataset,path)
            self.previews._visible_path(dataset,path)
            source,relative=self.previews._source(dataset,path)
            if location is not None and source!=location:
                raise ScientificPreviewRejected("Ripple 配对文件不在同一个已授权的只读位置。")
            location=source
            entries.append({"key":key,"path":path,"relative":relative,"declaration":declared.model_dump(mode="json")})
        signature=json.dumps([row["dataset_id"],row["path"],location.model_dump(mode="json"),entries],sort_keys=True,separators=(",",":"))
        return entries,location,signature

    async def initialize(self):
        # Same two-key resource budget as ENVI, not its format/name discovery.
        await super().initialize()
        self.info=self.info.model_copy(update={"filename":"cube.rpl","metadata":{**self.info.metadata,"source":"dataset_ripple_scope"}})
        return self


async def ripple_visualization(file_service,catalog,image,file_id,user_id,request,*,scope_factory=RippleDatasetScope,worker=None):
    plugin=await catalog.require_enabled(user_id,request.plugin_id)
    if plugin.reader!="ripple-window" or plugin.adapter!="ripple-window" or request.operation!="preview":
        raise ScientificPreviewRejected("此插件未获得 Ripple 双文件读取能力。")
    if not isinstance(file_id,str) or not file_id.startswith(PREFIX):
        raise ScientificPreviewRejected("Ripple 需从数据集内的 .rpl 与同名 .raw 配对打开，不支持孤立头文件上传。")
    kind=request.kind or "tree"
    try: validate_ripple_window_options(kind,request.options)
    except (ValueError,TypeError):
        raise ScientificPreviewRejected("Ripple 通道、像元或区域选择无效。") from None
    if kind!="tree" and request.version is None:
        raise ScientificPreviewRejected("请先读取 Ripple 结构，再携带配对文件版本读取数值窗口。")
    previews=getattr(file_service._file_storage,"previews",None)
    if previews is None:
        raise ScientificPreviewRejected("当前存储没有受控数据集配对读取能力。")
    async with asyncio.timeout(70):
        scope=await scope_factory(previews,file_id,user_id).initialize()
        await catalog.require_enabled(user_id,request.plugin_id)
        return await window_visualization(scope,catalog,image,file_id,user_id,request,resources=scope.resources,
            final_fence=scope.verify_snapshot,**({"worker":worker} if worker is not None else {}))
