"""Original synthetic ODIM 2.4 fixtures, not operational radar data."""
import io


def radar_bytes(*, dtype="u1", compression="gzip", sweeps=2, gain=.5, offset=-32., a1gate=3,
                rstart=1000., rscale=250., rows=8, columns=12):
    import h5py
    import numpy as np
    buffer = io.BytesIO()
    with h5py.File(buffer, "w") as handle:
        def attributes(group, **values):
            for key, value in values.items(): group.attrs[key] = np.bytes_(value) if isinstance(value, str) else value
        attributes(handle, Conventions="ODIM_H5/V2_4")
        what = handle.create_group("what")
        attributes(what, object="PVOL" if sweeps > 1 else "SCAN", version="H5rad 2.4", date="20260911", time="010000", source="NOD:synthetic")
        attributes(handle.create_group("where"), lon=120., lat=30., height=42.)
        attributes(handle.create_group("how"), comment="/private/DO-NOT-EXPOSE-RADAR-SOURCE")
        for i in range(1, sweeps+1):
            sweep = handle.create_group("dataset"+str(i))
            attributes(sweep.create_group("what"), product="SCAN", startdate="20260911", starttime="010000", enddate="20260911", endtime="010008")
            attributes(sweep.create_group("where"), elangle=float(i)*.5, nrays=rows, nbins=columns, a1gate=a1gate, rstart=rstart, rscale=rscale)
            sweep.create_group("how")
            for j, quantity in enumerate(("DBZH", "VRADH"), 1):
                data_group = sweep.create_group("data"+str(j))
                maximum = 255 if np.dtype(dtype).itemsize == 1 else 65535
                raw = np.fromfunction(lambda r,c: (i-1)*100+(j-1)*20+5+r*10+c, (rows,columns), dtype=int).astype(dtype)
                raw[1,1], raw[1,2] = maximum, 0
                attributes(data_group.create_group("what"), quantity=quantity, gain=gain, offset=offset, nodata=float(maximum), undetect=0.)
                dataset = data_group.create_dataset("data", data=raw, compression=compression,
                    chunks=(min(rows,4), min(columns,6)) if compression else None)
                if np.dtype(dtype).itemsize == 1: attributes(dataset, CLASS="IMAGE", IMAGE_VERSION="1.2")
    return buffer.getvalue()


def mutate(data, operation):
    import h5py
    buffer = io.BytesIO(data)
    with h5py.File(buffer, "r+") as handle: operation(handle)
    return buffer.getvalue()


SELECTION = {"sweep": 1, "quantity": "DBZH", "ray_start": 1, "ray_count": 2,
             "gate_start": 1, "gate_count": 4, "decode": "raw"}


def preview(data, kind="tree", options=None, limits=None, fmt="h5"):
    from app.services.radar_window_reader import radar_window_preview
    calls = []
    def read(start, length): calls.append((start,length)); return data[start:start+length]
    return radar_window_preview(read, len(data), fmt, kind, options, limits), calls


def browser_payloads():
    result = {}
    for name, gain in (("positive", .5), ("negative", -.25)):
        data = radar_bytes(gain=gain)
        result[name] = {"tree": preview(data)[0], "image": preview(data,"image",SELECTION)[0]}
    return result


if __name__ == "__main__":
    import json
    print(json.dumps(browser_payloads(), ensure_ascii=False, allow_nan=False))
