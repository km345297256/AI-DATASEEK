"""Keep host and worker pure schemas aligned without importing the native parser."""
import ast
import importlib.util
from pathlib import Path
import pytest
from app.services import czi_payload, geo_format_readers

ROOT = Path(__file__).resolve().parents[2]


def host_module(filename):
    path = ROOT / "backend/app/application/services" / filename
    if not path.is_file():
        pytest.skip("cross-tree parity requires the full repository checkout")
    spec = importlib.util.spec_from_file_location("host_" + filename[:-3], path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module, path


@pytest.mark.parametrize("worker,filename,functions", [(geo_format_readers, "geo_visualization.py", ["_extent", "validate_geo_options", "validate_geo_payload"]), (czi_payload, "czi_visualization.py", ["validate_czi_options", "validate_czi_payload"])])
def test_pure_validation_functions_are_ast_identical(worker, filename, functions):
    host, path = host_module(filename)
    def definitions(source):
        return {node.name: ast.dump(node, include_attributes=False) for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
    left, right = definitions(Path(worker.__file__).read_text()), definitions(path.read_text())
    for name in functions:
        assert left[name] == right[name], f"Host/worker schema drift in {name}"


def test_real_geo_worker_results_cross_host_boundary():
    host, _ = host_module("geo_visualization.py")
    fixtures = [(b"ncols 2 nrows 2 xllcorner 0 yllcorner 0 cellsize 1 1 2 3 4", "asc"),
                (b"DSAA 2 2 0 1 0 1 1 4 1 2 3 4", "grd"),
                (b'<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><Point><coordinates>1,2</coordinates></Point></Placemark></kml>', "kml")]
    for data, fmt in fixtures:
        result = geo_format_readers.geoformat_preview(data, fmt, {})
        assert host.validate_geo_payload(result) is result


def test_option_schema_parity_valid_and_hostile():
    geo, _ = host_module("geo_visualization.py")
    czi, _ = host_module("czi_visualization.py")
    for value in [{}, {"crs": "EPSG:4326"}, {"crs": "EPSG:3857"}]:
        assert geo.validate_geo_options(value) == geo_format_readers.validate_geo_options(value)
    for value in [{"crs": []}, {"path": "/tmp/a"}, {"crs": "auto"}]:
        for fn in [geo.validate_geo_options, geo_format_readers.validate_geo_options]:
            with pytest.raises(ValueError): fn(value)
    for kind, value in [("tree", {}), ("image", {"indices": [0, 0, 0], "roi": [0, 0, 1, 1]})]:
        assert czi.validate_czi_options(kind, value) == czi_payload.validate_czi_options(kind, value)
