"""Explicit optional xradar 0.10.0 oracle; never imported by production or test discovery.

Uses the official reader unchanged on original ODIM 2.4 fixtures. The xradar
writer currently writes ODIM 2.2, so this deliberately makes no 2.4 writer claim.
"""
import io
import json
from importlib.metadata import version
import numpy as np
from xradar.io.backends.odim import OdimBackendEntrypoint
from radar_window_fixtures import radar_bytes, preview, SELECTION


def main():
    assert version("xradar") == "0.10.0"
    cases = 0
    for dtype in ("u1", "<u2", ">u2"):
        for compression in (None, "gzip"):
            for gain in (.5, -.25):
                data = radar_bytes(dtype=dtype, compression=compression, gain=gain)
                own, _ = preview(data, "image", SELECTION)
                for physical in (False, True):
                    with OdimBackendEntrypoint().open_dataset(io.BytesIO(data), group="sweep_0", mask_and_scale=physical, decode_times=False) as reference:
                        field = reference["DBZH"]
                        np.testing.assert_array_equal(reference["range"].values[1:5], own["radar"]["range_m"])
                        raw = np.asarray(own["array"]["values"]).reshape(2, 4)
                        actual = field.values[1:3, 1:5]
                        quantity = own["choices"]["sweeps"][0]["quantities"][0]
                        if physical:
                            valid = (raw != quantity["nodata"]) & (raw != quantity["undetect"])
                            np.testing.assert_allclose(actual[valid], quantity["offset"] + quantity["gain"] * raw[valid], rtol=0, atol=0)
                            assert np.isnan(actual[0, 0])
                            # xarray's ODIM decoding masks nodata; do not claim it masks
                            # the separate _Undetect declaration. Our UI classifies both.
                            assert field.encoding["scale_factor"] == gain
                        else:
                            np.testing.assert_array_equal(actual, raw)
                            assert field.attrs["scale_factor"] == gain
                            assert field.attrs["add_offset"] == -32
                            assert field.attrs["_FillValue"] == quantity["nodata"]
                            assert field.attrs["_Undetect"] == 0
                        cases += 1
    print(json.dumps({"oracle": "unmodified xradar 0.10.0 OdimBackendEntrypoint", "cases": cases,
        "range_centres_m": [1375, 1625, 1875, 2125], "raw_codes_and_declared_gain_offset": True,
        "numpy": version("numpy"), "xarray": version("xarray"), "h5netcdf": version("h5netcdf")}, allow_nan=False))


if __name__ == "__main__": main()
