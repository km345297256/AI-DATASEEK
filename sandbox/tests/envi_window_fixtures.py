"""Synthetic ENVI pairs; never reads project datasets."""
import numpy as np
from app.services.envi_window_reader import envi_window_preview

def fixture(interleave="bsq", dtype="<f4", wavelengths=True):
    values = np.array([[[b*100+y*10+x for x in range(4)] for y in range(3)] for b in range(5)], dtype=dtype)
    code = {"u1": 1, "i2": 2, "i4": 3, "f4": 4, "f8": 5, "u2": 12, "u4": 13}[values.dtype.kind+str(values.dtype.itemsize)]
    header = f"ENVI\nsamples = 4\nlines = 3\nbands = 5\nheader offset = 8\nfile type = ENVI Standard\ndata type = {code}\ninterleave = {interleave}\nbyte order = {int(values.dtype.byteorder == '>')}\n"
    if wavelengths:
        header += "wavelength = {400, 500, 600, 700, 800}\nwavelength units = Nanometers\n"
    ordered = values if interleave == "bsq" else values.transpose(1, 0, 2) if interleave == "bil" else values.transpose(1, 2, 0)
    return header.encode(), b"\0"*8+ordered.tobytes(), values

def preview(header, data, kind="tree", options=None, **kwargs):
    resources = [{"key": "header", "offset": 0, "size": len(header)}, {"key": "data", "offset": len(header), "size": len(data)}]
    calls = []
    def read(offset, length):
        calls.append((offset, length))
        assert (0 <= offset and offset+length <= len(header)) or (len(header) <= offset and offset+length <= len(header)+len(data))
        return header[offset:offset+length] if offset < len(header) else data[offset-len(header):offset-len(header)+length]
    return envi_window_preview(read, len(header)+len(data), resources, kind, options, **kwargs), calls

def browser_payloads():
    header, data, _ = fixture("bip")
    return {kind: preview(header, data, kind, options)[0] for kind, options in (
        ("tree", {}), ("image", {"band": 2, "x": 1, "y": 1, "width": 3, "height": 2}),
        ("series", {"x": 2, "y": 1, "band_start": 0, "band_count": 5}))}

if __name__ == "__main__":
    import json
    print(json.dumps(browser_payloads(), ensure_ascii=False))
