"""The host must not advertise image formats its byte validator cannot handle.

Read the host's literal contract with AST rather than importing its application
or dependencies into the sandbox test process. Image implementation coverage is
the raster/vector dispatch registries, not a second hardcoded extension list.
"""
import ast
from pathlib import Path

from app.services import artifact_validation


def test_host_image_contract_matches_sandbox_validator_dispatch():
    source = Path(__file__).resolve().parents[2] / "backend/app/domain/services/analysis_completion.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    assignments = [node for node in module.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "KINDS" for target in node.targets)]
    assert len(assignments) == 1, "Expected one literal host artifact-kind contract"
    kinds = ast.literal_eval(assignments[0].value)
    declared = kinds["image"]
    implemented = {suffix.removeprefix(".") for suffix in (
        artifact_validation.IMAGE_FORMATS.keys() | artifact_validation.VECTOR_IMAGE_SUFFIXES
    )}
    assert declared == implemented, (
        f"Host-only image formats: {sorted(declared - implemented)}; "
        f"Validator-only image formats: {sorted(implemented - declared)}"
    )
    for extension in declared:
        assert artifact_validation._classify(f"figure.{extension}", "image") == "image"
        assert artifact_validation._classify(f"figure.{extension.upper()}", "image") == "image"
