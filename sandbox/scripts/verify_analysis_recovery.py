"""Synthetic production-image smoke: real process receipt + Excel + PNG bytes.

No model/provider call, user dataset, uploaded file or persistent DB is touched.
Use in a disposable sandbox-image container, with no published ports.
"""
import asyncio
import json
from pathlib import Path
import shlex
import tempfile
from uuid import uuid4

import pandas as pd

from app.services.artifact_validation import validate_artifacts
from app.services.shell import ShellService


async def main():
    root = Path("/home/ubuntu/output")
    root.mkdir(parents=True, exist_ok=True)
    service = ShellService()
    result = {}
    with tempfile.TemporaryDirectory(prefix="recovery-smoke-", dir=root) as temporary:
        directory = Path(temporary)
        source = directory / "synthetic.xlsx"
        pd.DataFrame({"group": ["a", "b", "c"] * 4,
                      "x": range(12), "y": [index * 2 + 1 for index in range(12)]}).to_excel(source, index=False)
        output = directory / "quicklook"
        commands = [("failed", "python -c 'raise SystemExit(3)'", 3),
                    ("render", f"ai-dataseek-quicklook {shlex.quote(str(source))} --output {shlex.quote(str(output))} --max-plots 1", 0)]
        for name, command, expected in commands:
            operation = uuid4().hex
            try:
                await service.exec_command(name, str(directory), command, operation_id=operation)
                await service.wait_for_process(name, 30, operation_id=operation)
                receipt = await service.operation_status(name, operation)
                assert receipt.state == "exited" and receipt.returncode == expected
                assert receipt.process_tree_quiescent is True
                result[name + "_confirmed"] = True
            finally:
                await service.release_shell(name, operation_id=operation)
        images = list(output.rglob("*.png"))
        assert len(images) == 1
        checked = validate_artifacts([{"path": str(path), "kind": "image"} for path in images])
        assert checked["version"] == 1 and all(item["valid"] for item in checked["files"])
        result.update(excel_analyzed=True, rendered_images=len(images), actual_png_validated=True)
    result["temporary_fixture_removed"] = not Path(temporary).exists()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
