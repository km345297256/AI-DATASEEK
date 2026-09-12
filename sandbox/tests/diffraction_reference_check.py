"""Explicit optional oracle: temporary installed upstream IO readers, no production dependency.

Run with --no-deps xrayutilities==1.8.0 and sasdata==0.11.0 in the science sandbox.
Load the unmodified official IO modules without the unrelated fitting package initializer.
No scientific functions or results are mocked; this does not test the full analysis suite.
Unlike standard tests this fails, never skips, if either upstream is unavailable.
"""
import tempfile
import sys
import importlib.util
from importlib.metadata import version as package_version, distribution
from pathlib import Path
import numpy as np
# Keep authentic package resource paths, but don't execute the top-level initializer
# that imports every fitting module and a separately compiled Fortran optimizer.
# The official config, helper and panalytical_xml modules execute unmodified below.
_package = Path(distribution("xrayutilities").locate_file("xrayutilities"))
for _name, _path in (("xrayutilities", _package), ("xrayutilities.io", _package / "io")):
    _spec = importlib.util.spec_from_file_location(_name, _path / "__init__.py", submodule_search_locations=[str(_path)])
    sys.modules[_name] = importlib.util.module_from_spec(_spec)
from xrayutilities.io.panalytical_xml import XRDMLFile
from sasdata.dataloader.readers.cansas_reader import Reader
from sasdata.dataloader.data_info import Data1D
from app.services.diffraction_reader import diffraction_preview
from diffraction_fixtures import xrdml

def main():
    assert package_version("xrayutilities") == "1.8.0"
    assert package_version("sasdata") == "0.11.0"
    cases = 0
    for version in ("1.0", "1.7", "2.0", "2.1", "2.4"):
        for explicit, coupled in ((False, False), (True, False), (False, True)):
            raw = xrdml(version=version, explicit=explicit, coupled=coupled)
            with tempfile.NamedTemporaryFile(suffix=".xrdml", prefix="dataseek-reference-") as file:
                file.write(raw); file.flush()
                oracle = XRDMLFile(file.name).scan
            for scan in (0, 1):
                our = diffraction_preview(raw, "xrdml", "series", {"scan": scan})["series"][0]
                np.testing.assert_array_equal(our["x"], oracle.scanmot[scan])
                if version.startswith("2."):
                    np.testing.assert_array_equal(our["y"], oracle.ddict["counts"][scan])
                else:
                    # Upstream intentionally normalizes detector by acquisition time;
                    # compare after restoring the declared time, not its processed view.
                    np.testing.assert_array_equal(our["y"], oracle.ddict["detector"][scan] * oracle.ddict["countTime"][scan])
                assert our["x_error"] is None and our["y_error"] is None
                cases += 1
    with tempfile.TemporaryDirectory(prefix="dataseek-diffraction-reference-") as temp:
        source = Data1D(x=np.array([.01,.021,.045,.1]), y=np.array([10.,-2.,5.,1.]), dx=np.array([.001,.0012,.0015,.002]), dy=np.array([.5,.2,.3,.1]))
        source.title = "Original synthetic oracle fixture"
        source.xaxis("Q", "1/A"); source.yaxis("Intensity", "1/cm")
        path = Path(temp) / "original-cansas.xml"
        Reader().write(str(path), source)
        raw = path.read_bytes()
        # The writer may include an optional XML stylesheet PI; the viewer profile
        # correctly forbids those. Remove only that non-data presentation instruction.
        from lxml import etree
        root = etree.fromstring(raw, parser=etree.XMLParser(resolve_entities=False, no_network=True))
        raw = etree.tostring(root, encoding="UTF-8")
        # Use the emitted XML for both parsers, preserving every scientific column.
        path.write_bytes(raw)
        references = Reader().read(str(path))
        assert len(references) == 1 and not references[0].errors, references[0].errors
        oracle = references[0]
        our = diffraction_preview(raw, "xml", "series", {"scan": 0})["series"][0]
        for field, upstream in (("x", "x"), ("y", "y"), ("x_error", "dx"), ("y_error", "dy")):
            np.testing.assert_array_equal(our[field], getattr(oracle, upstream))
        cases += 1
    print(f"PASS: {cases} real upstream reader comparisons (xrayutilities 1.8.0; sasdata 0.11.0)")

if __name__ == "__main__": main()
