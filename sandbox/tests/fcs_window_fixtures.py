"""Independent synthetic FCS writers, never import the production parser."""
import io
import struct


def fcs_bytes(rows=((1.25, 10.0), (-2.5, 20.0), (0.0, 30.0), (4.75, 40.0)), *, datatype="F", bits=None, endian="<", version="3.1", names=None, metadata=None, extra_pairs=(), delimiter="|", virtual_total=None):
    count = len(rows) if virtual_total is None else virtual_total
    columns = len(rows[0]); names = names or ["FSC-A", "CD3-A"][:columns]
    if len(names) != columns: names = ["P"+str(i+1) for i in range(columns)]
    bits = bits or (32 if datatype != "D" else 64)
    widths = bits if isinstance(bits, list) else [bits]*columns
    stride = sum(widths)//8
    text = {"$BEGINANALYSIS": "0", "$BEGINDATA": "0000000000", "$BEGINSTEXT": "0", "$BYTEORD": "1,2,3,4" if endian == "<" else "4,3,2,1",
        "$DATATYPE": datatype, "$ENDANALYSIS": "0", "$ENDDATA": "0000000000", "$ENDSTEXT": "0", "$MODE": "L", "$NEXTDATA": "0", "$PAR": str(columns), "$TOT": str(count)}
    for index in range(columns):
        p = "$P"+str(index+1); text.update({p+"B": str(widths[index]), p+"R": str(2**widths[index] if datatype == "I" else 262144), p+"E": "0,0", p+"N": names[index]})
    text.update(metadata or {})
    def encode(): return (delimiter+delimiter.join(k.replace(delimiter, delimiter*2)+delimiter+v.replace(delimiter, delimiter*2) for k, v in [*text.items(), *extra_pairs])+delimiter).encode("utf-8")
    raw_text = encode(); start = 256+len(raw_text); end = start+count*stride-1
    if not metadata or "$BEGINDATA" not in metadata: text["$BEGINDATA"] = str(start).zfill(10)
    if not metadata or "$ENDDATA" not in metadata: text["$ENDDATA"] = str(end).zfill(10)
    raw_text = encode(); assert start == 256+len(raw_text)
    data_offsets = [start, end] if end <= 99999999 else [0, 0]
    header = ("FCS"+version+"    "+"".join(str(n).rjust(8) for n in [256, start-1, *data_offsets, 0, 0])).encode("ascii")
    prefix = header+b" "*(256-len(header))+raw_text
    if virtual_total is not None: return prefix, end+1
    codes = [("f" if datatype == "F" else "d" if datatype == "D" else {8: "B", 16: "H", 32: "I", 64: "Q"}.get(width, "H")) for width in widths]
    return prefix+b"".join(struct.pack(endian+"".join(codes), *row) for row in rows)


def official_fcs():
    import flowio
    output = io.BytesIO()
    flowio.create_fcs(output, [1.25, 10, -2.5, 20, 0, 30, 4.75, 40], ["FSC-A", "CD3-A"], ["散射", "CD3 PE"],
        metadata_dict={"p1g": "2", "p1calibration": "3,MESF", "p2d": "Linear,0,100", "spillover": "2,FSC-A,CD3-A,1,0.1,0.03,1", "src": "private-patient", "fil": "/private/never-return"})
    return output.getvalue()


class SparseFcs(io.RawIOBase):
    """Large logical FCS for independent metadata-only reader and selected ranges."""
    def __init__(self, prefix, size): super().__init__(); self.prefix, self.size, self.position, self.reads = prefix, size, 0, []
    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.position
    def seek(self, offset, whence=0):
        self.position = offset if whence == 0 else self.position+offset if whence == 1 else self.size+offset
        return self.position
    def read(self, length=-1):
        if length < 0 or length > 1048576: raise ValueError("no whole-source read in synthetic oracle")
        start = self.position; length = min(length, max(0, self.size-start)); self.position += length; self.reads.append((start, length))
        if start+length <= len(self.prefix): return self.prefix[start:start+length]
        if start >= len(self.prefix): return bytes(length)
        return self.prefix[start:]+bytes(start+length-len(self.prefix))
