import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from app.services.radar_window_reader import radar_window_preview
from app.services.radar_window_payload import ERROR, validate_radar_window_options, validate_radar_window_payload
from radar_window_fixtures import SELECTION, radar_bytes, mutate, preview


class RadarWindowTests(unittest.TestCase):
    def test_real_hdf5_variants_and_exact_selection(self):
        for dtype in ("u1", "<u2", ">u2"):
            for compression in (None,"gzip"):
                for gain in (.5,-.25):
                    with self.subTest(dtype=dtype, compression=compression, gain=gain):
                        data=radar_bytes(dtype=dtype,compression=compression,gain=gain)
                        tree,calls=preview(data)
                        self.assertEqual(tree["metadata"]["numeric_bytes_read"],0)
                        self.assertNotIn("array",tree)
                        image,calls=preview(data,"image",SELECTION)
                        self.assertEqual(image["array"]["values"],[255 if dtype=="u1" else 65535,0,18,19,26,27,28,29])
                        self.assertEqual(image["radar"],{"range_m":[1375.,1625.,1875.,2125.],"ray_indices":[1,2],"nodata_count":1,"undetect_count":1})
                        self.assertEqual(image["choices"]["sweeps"][0]["a1gate"],3)
                        self.assertEqual(image["metadata"]["read_bytes"],sum(n for _,n in calls))
                        self.assertEqual(image["metadata"]["read_requests"],len(calls))
                        self.assertTrue(all(n<=1048576 and 0<=a<=a+n<=len(data) for a,n in calls))
                        self.assertNotIn("DO-NOT-EXPOSE",json.dumps(image))

    def test_tree_never_decodes_numerical_dataset(self):
        import h5py
        data=radar_bytes()
        with patch.object(h5py.Dataset,"__getitem__",side_effect=AssertionError("dataset decode")):
            self.assertEqual(preview(data)[0]["metadata"]["numeric_bytes_read"],0)

    def test_invalid_options_fail_before_any_read(self):
        bad=[{}, {**SELECTION,"sweep":True}, {**SELECTION,"ray_count":129}, {**SELECTION,"gate_count":0},
             {**SELECTION,"gate_start":-1}, {**SELECTION,"quantity":"OTHER"}, {**SELECTION,"decode":"physical"}, {**SELECTION,"path":"/private/x"}]
        for options in bad:
            with self.subTest(options=options):
                calls=[]
                with self.assertRaisesRegex(ValueError,"ODIM"):radar_window_preview(lambda a,n:calls.append((a,n)),4096,"h5","image",options)
                self.assertFalse(calls)
        for kind,options in (("geometry",{}),("tree",SELECTION),(True,{})):
            with self.assertRaises(ValueError):validate_radar_window_options(kind,options)

    def test_malformed_attrs_rejected_before_native_array_decode(self):
        import h5py
        import numpy as np
        original=radar_bytes()
        edits=[lambda h:h.attrs.__setitem__("Conventions",np.bytes_("ODIM_H5/V2_3")),
          lambda h:h["what"].attrs.__setitem__("object",np.bytes_("COMP")),
          lambda h:h["dataset1/what"].attrs.__setitem__("product",np.bytes_("PPI")),
          lambda h:h["dataset1/where"].attrs.__setitem__("rstart",float("nan")),
          lambda h:h["dataset1/where"].attrs.__setitem__("rscale",0.),
          lambda h:h["dataset1/where"].attrs.__setitem__("a1gate",8),
          lambda h:h["dataset1/where"].attrs.__setitem__("nrays",8.),
          lambda h:h["dataset1/where"].attrs.__setitem__("elangle",91.),
          lambda h:h["dataset1/data1/what"].attrs.__setitem__("gain",0.),
          lambda h:h["dataset1/data1/what"].attrs.__setitem__("offset",1e20),
          lambda h:h["dataset1/data1/what"].attrs.__setitem__("nodata",1.5),
          lambda h:h["dataset1/data1/what"].attrs.__setitem__("undetect",255.),
          lambda h:h["dataset1/data1/what"].attrs.__setitem__("quantity","DBZH"),  # vlen text
          lambda h:h["dataset1/data1/what"].attrs.__delitem__("gain"),
          lambda h:h["dataset1/data1"].create_group("where"),
          lambda h:h["dataset1/data1/data"].attrs.__setitem__("scale_factor",2.),
          lambda h:h["dataset1/data2/what"].attrs.__setitem__("quantity",np.bytes_("DBZH"))]
        for i,edit in enumerate(edits):
            with self.subTest(edit=i):
                data=mutate(original,edit)
                with patch.object(h5py.Dataset,"__getitem__",side_effect=AssertionError("dataset decode")) as decode:
                    with self.assertRaisesRegex(ValueError,"ODIM"):preview(data,"image",SELECTION)
                    decode.assert_not_called()

    def test_large_source_has_no_whole_file_fallback(self):
        # Valid HDF5 with inert trailing bytes. This tests transport bounds, not a large radar cube.
        data=radar_bytes()+bytes(16*1024**2)
        for kind,options in (("tree",{}),("image",SELECTION)):
            with self.subTest(kind=kind):
                result,calls=preview(data,kind,options)
                self.assertEqual(result["metadata"]["source_bytes"],len(data))
                self.assertLess(sum(n for _,n in calls),128*1024)
                self.assertLess(max(n for _,n in calls),len(data))

    def test_external_soft_alias_virtual_dtype_and_chunk_budgets(self):
        import h5py
        import numpy as np
        original=radar_bytes()
        def replace(h,kind):
            g=h["dataset1/data1"];del g["data"]
            if kind=="external":g["data"]=h5py.ExternalLink("/private/secret","/values")
            elif kind=="soft":g["data"]=h5py.SoftLink("/dataset1/data2/data")
            elif kind=="alias":g["data"]=h["dataset1/data2/data"]
            elif kind=="virtual":
                layout=h5py.VirtualLayout((8,12),dtype="u1");layout[:]=h5py.VirtualSource("/private/secret","/values",shape=(8,12));g.create_virtual_dataset("data",layout)
            elif kind=="float":g.create_dataset("data",data=np.zeros((8,12),dtype="f4"))
            elif kind=="unallocated":g.create_dataset("data",shape=(8,12),dtype="u1",chunks=(4,6))
        for kind in ("external","soft","alias","virtual","float","unallocated"):
            with self.subTest(kind=kind):
                data=mutate(original,lambda h:replace(h,kind))
                with self.assertRaisesRegex(ValueError,"ODIM"):preview(data,"image",SELECTION)
        with self.assertRaises(ValueError):preview(original,"image",SELECTION,{"max_read_bytes":1,"max_total_bytes":1,"max_reads":1})

    def test_chunk_stream_expansion_preflight(self):
        import zlib
        data=radar_bytes()
        def edit(h):h["dataset1/data1/data"].id.write_direct_chunk((0,0),zlib.compress(bytes(25)))  # Declared 4*6=24 bytes.
        data=mutate(data,edit)
        with self.assertRaisesRegex(ValueError,"ODIM"):preview(data,"image",SELECTION)

    def test_out_of_range_and_empty_chunk_not_filled(self):
        data=radar_bytes()
        for edit in ({"ray_start":7},{"gate_start":11},{"sweep":3},{"quantity":"TH"}):
            with self.subTest(edit=edit),self.assertRaises(ValueError):preview(data,"image",{**SELECTION,**edit})

    def test_pure_validator_copy_and_reject_request_or_source_mismatch(self):
        sandbox=Path(__file__).resolve().parents[1]
        backend=sandbox.parent/"backend/app/application/services/radar_window_visualization.py"
        self.assertEqual((sandbox/"app/services/radar_window_payload.py").read_text().rstrip(),backend.read_text().rstrip())
        data=radar_bytes();value,_=preview(data,"image",SELECTION)
        scope={};exec(compile(backend.read_text(),str(backend),"exec"),scope)
        for validator in (validate_radar_window_payload,scope["validate_radar_window_payload"]):
            self.assertIs(validator(value,kind="image",options=SELECTION,source_bytes=len(data)),value)
            for binding in ({"kind":"tree"},{"options":{**SELECTION,"ray_start":0}},{"source_bytes":len(data)+1},{"fmt":"nc"},{"read_requests":True}):
                with self.subTest(binding=binding),self.assertRaises(ValueError):validator(value,**binding)

    def test_strict_payload_types_counts_and_range_semantics(self):
        original=preview(radar_bytes(),"image",SELECTION)[0]
        edits=[lambda r:r.__setitem__("contract_version",2.),lambda r:r.__setitem__("kind","tree"),
          lambda r:r["array"]["values"].__setitem__(0,True),lambda r:r["array"]["values"].__setitem__(0,1.5),
          lambda r:r["array"]["values"].__setitem__(0,256),lambda r:r["radar"]["range_m"].__setitem__(0,1250),
          lambda r:r["radar"].__setitem__("nodata_count",0),lambda r:r["radar"]["ray_indices"].__setitem__(0,True),
          lambda r:r["choices"]["sweeps"][0].__setitem__("a1gate",True),
          lambda r:r["choices"]["sweeps"][0]["quantities"][0].__setitem__("gain",False),
          lambda r:r["metadata"].__setitem__("source_bytes",True),lambda r:r["metadata"]["limits"].__setitem__("max_rays",True),
          lambda r:r["metadata"].__setitem__("chunks_touched",2),lambda r:r["metadata"].__setitem__("path","/private/file")]
        for i,edit in enumerate(edits):
            value=copy.deepcopy(original);edit(value)
            with self.subTest(edit=i),self.assertRaises(ValueError):validate_radar_window_payload(value)

    def test_cancellation_and_error_path_sanitized(self):
        for exception in (RuntimeError("/private/SECRET"),asyncio.CancelledError()):
            def read(a,n):raise exception
            if isinstance(exception,Exception):
                with self.assertRaises(ValueError) as caught:radar_window_preview(read,1000,"h5")
                self.assertEqual(str(caught.exception),ERROR)
            else:
                with self.assertRaises(asyncio.CancelledError):radar_window_preview(read,1000,"h5")


if __name__ == "__main__": unittest.main()
