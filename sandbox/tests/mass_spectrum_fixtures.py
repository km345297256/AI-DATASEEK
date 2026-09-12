"""Small spec-shaped synthetic spectra, cross-checked with Pyteomics in tests."""
import base64
import json
import struct
import zlib


def psims_mzml_bytes():
    """Official writer, bundled offline CV, no real instrument or user source."""
    import io
    import numpy as np
    from psims.mzml.writer import MzMLWriter
    from psims.controlled_vocabulary.controlled_vocabulary import OBOCache
    output = io.BytesIO()
    with MzMLWriter(output, close=False, vocabulary_resolver=OBOCache(enabled=False, use_remote=False)) as writer:
        writer.controlled_vocabularies()
        writer.file_description(["MS1 spectrum", "profile spectrum"], [])
        writer.software_list([{"id": "synthetic-software", "version": "1.0", "params": ["custom unreleased software tool"]}])
        components = [writer.Source(1, ["ionization type"]), writer.Analyzer(2, ["mass analyzer type"]), writer.Detector(3, ["detector type"])]
        writer.instrument_configuration_list([writer.InstrumentConfiguration("IC1", components, ["instrument model"], software_reference="synthetic-software")])
        writer.data_processing_list([writer.DataProcessing([writer.ProcessingMethod(0, "synthetic-software", ["Conversion to mzML"])], id="DP1")])
        with writer.run(id="synthetic-run", instrument_configuration="IC1"):
            with writer.spectrum_list(count=1, data_processing_method="DP1"):
                writer.write_spectrum(np.array([100, 100.25, 100.5, 100.75]), np.array([-1, 3, 7, 2]), id="scan=1", centroided=False,
                    params=["MS1 spectrum", {"ms level": 1}], scan_start_time=2.5, compression="zlib")
    return output.getvalue()


def mgf_bytes(count=2):
    return ("# synthetic only\n" + "".join(f"BEGIN IONS\nTITLE=/private/hidden-{i}\nPEPMASS=445.25\nRTINSECONDS=12.5\n150.5 10\n100.25 25\n200.75 5\nEND IONS\n" for i in range(count))).encode()


def cv(accession, name, value="", **attrs):
    tail = "".join(f' {k}="{v}"' for k, v in attrs.items())
    return f'<cvParam cvRef="MS" accession="{accession}" name="{name}" value="{value}"{tail}/>'


def binary(values, role, *, width=8, compressed=False):
    raw = struct.pack("<" + ("f" if width == 4 else "d") * len(values), *values)
    encoded = base64.b64encode(zlib.compress(raw) if compressed else raw).decode()
    terms = cv("MS:1000514" if role == "mz" else "MS:1000515", "m/z array" if role == "mz" else "intensity array",
               unitCvRef="MS", unitAccession="MS:1000040" if role == "mz" else "MS:1000131", unitName="m/z" if role == "mz" else "number of detector counts")
    terms += cv("MS:1000521" if width == 4 else "MS:1000523", "32-bit float" if width == 4 else "64-bit float")
    terms += cv("MS:1000574" if compressed else "MS:1000576", "zlib compression" if compressed else "no compression")
    return f'<binaryDataArray encodedLength="{len(encoded)}">{terms}<binary>{encoded}</binary></binaryDataArray>'


def mzml_bytes(*, width=8, compressed=False, profile=True, indexed=False, mz=None, intensity=None):
    mz = [100, 100.25, 100.5, 100.75] if mz is None else mz
    intensity = [-1, 3, 7, 2] if intensity is None else intensity
    term = cv("MS:1000128" if profile else "MS:1000127", "profile spectrum" if profile else "centroid spectrum")
    term += cv("MS:1000511", "ms level", "1")
    scan = cv("MS:1000016", "scan start time", "2.5", unitCvRef="UO", unitAccession="UO:0000031", unitName="minute")
    arrays = binary(mz, "mz", width=width, compressed=compressed) + binary(intensity, "intensity", width=width, compressed=compressed)
    xml = f'<mzML xmlns="http://psi.hupo.org/ms/mzml" version="1.1.0"><cvList count="2"><cv id="MS" fullName="PSI-MS" version="4.1" URI="https://example.invalid/descriptive-cv-only"/><cv id="UO" fullName="Units" version="1" URI="https://example.invalid/units"/></cvList><run id="synthetic"><spectrumList count="1"><spectrum id="controllerType=0 controllerNumber=1 scan=1" index="0" defaultArrayLength="{len(mz)}">{term}<scanList count="1"><scan>{scan}</scan></scanList><binaryDataArrayList count="2">{arrays}</binaryDataArrayList></spectrum></spectrumList></run></mzML>'
    if indexed: xml = '<indexedmzML xmlns="http://psi.hupo.org/ms/mzml">' + xml + '<indexList count="0"/><indexListOffset>0</indexListOffset><fileChecksum>ignored-descriptive-checksum</fileChecksum></indexedmzML>'
    return xml.encode()


def browser_payloads():
    from app.services.mass_spectrum_reader import mass_spectrum_preview
    mgf, mzml, pages = mgf_bytes(), mzml_bytes(width=4, compressed=True), mgf_bytes(65)
    return {"mgf_tree": mass_spectrum_preview(mgf, "mgf"), "mgf_series": mass_spectrum_preview(mgf, "mgf", "series", {"spectrum": "s-000000"}),
            "mzml_tree": mass_spectrum_preview(mzml, "mzml"), "mzml_series": mass_spectrum_preview(mzml, "mzml", "series", {"spectrum": "s-000000"}),
            "page_first": mass_spectrum_preview(pages, "mgf"), "page_second": mass_spectrum_preview(pages, "mgf", "tree", {"offset": 64})}


if __name__ == "__main__":
    print(json.dumps(browser_payloads(), ensure_ascii=False, allow_nan=False))
