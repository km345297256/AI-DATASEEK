"""Main parity plus isolation/selection regressions; runnable without pytest."""
import copy
import io
import json
import unittest
import zipfile

import h5py
import numpy as np
from scipy import io as sio, sparse

from app.services.main_matrix_reader import main_matrix_preview
from app.services.main_matrix_payload import validate_main_matrix_payload, validate_main_matrix_options


def fixture(value, fmt="npy", **kwargs):
    stream = io.BytesIO()
    if fmt == "npy": np.save(stream, value)
    elif fmt == "npz":
        if sparse.issparse(value): sparse.save_npz(stream, value)
        else: np.savez_compressed(stream, **value)
    elif fmt == "mat": sio.savemat(stream, value, **kwargs)
    elif fmt == "mtx": sio.mmwrite(stream, value, **kwargs)
    return stream.getvalue()


def options(shape, **kwargs):
    return {"variable": "array-0", "axes": [0, 1], "indices": [0] * len(shape), "component": "real", "row_range": None, "column_range": None, "max_points": 256, "structure": False, **kwargs}


class MatrixMigrationTests(unittest.TestCase):
    def test_main_tensor_axes_and_classic_mat(self):
        v = np.arange(120).reshape(2, 3, 4, 5)
        for fmt in ("npy", "npz", "mat"):
            with self.subTest(fmt=fmt):
                data = fixture(v if fmt == "npy" else {"tensor": v}, fmt)
                p = main_matrix_preview(data, fmt, "image", options(v.shape, axes=[3, 1], indices=[1, 0, 2, 0]))
                np.testing.assert_array_equal(p["matrix"]["plane"]["values"], v[1, :, 2, :].T)
                self.assertNotIn("path", p)

    def test_mat73_reversed_axes_no_external_links(self):
        b = io.BytesIO(); v = np.arange(60).reshape(3, 4, 5)
        with h5py.File(b, "w") as f:
            f["tensor"] = v.T
            f["external"] = h5py.ExternalLink("/etc/passwd", "secret")
            f["soft"] = h5py.SoftLink("/tensor")
        d = b.getvalue(); tree = main_matrix_preview(d, "mat")
        self.assertEqual(len(tree["matrix"]["arrays"]), 1)
        self.assertEqual(tree["matrix"]["arrays"][0]["shape"], [3, 4, 5])
        p = main_matrix_preview(d, "mat", "image", options(v.shape, axes=[0, 2], indices=[0, 2, 0]))
        np.testing.assert_array_equal(p["matrix"]["plane"]["values"], v[:, 2, :])

    def test_mat73_complex_compressed(self):
        b = io.BytesIO(); v = (np.arange(60) + np.arange(60)*2j).reshape(3, 4, 5)
        with h5py.File(b, "w") as f: f.create_dataset("tensor", data=v.T, chunks=(5, 2, 3), compression="gzip", shuffle=True, fletcher32=True)
        p = main_matrix_preview(b.getvalue(), "mat", "image", options(v.shape, axes=[2, 0], indices=[0, 2, 0], component="imaginary"))
        np.testing.assert_array_equal(p["matrix"]["plane"]["values"], v[:, 2, :].imag.T)

    def test_sparse_all_scipy_formats(self):
        base = sparse.eye(10000, format="coo")
        for fmt in ("csr", "csc", "coo", "dia", "bsr"):
            with self.subTest(fmt=fmt):
                d = fixture(base.asformat(fmt), "npz")
                p = main_matrix_preview(d, "npz", "image", options(base.shape))
                self.assertTrue(p["sampled"])
                self.assertEqual(p["matrix"]["plane"]["values"][0][0], 1)

    def test_sparse_structure_preserves_off_grid_and_duplicate_cancellation(self):
        value = sparse.coo_matrix(([7., -4., 9., -9.], ([301, 309, 2, 2], [451, 459, 3, 3])), shape=(10000, 10000))
        d = fixture(value, "npz")
        p = main_matrix_preview(d, "npz", "image", options(value.shape, structure=True))["matrix"]["plane"]
        self.assertGreater(sum(sum(row) for row in p["values"]), 0)
        self.assertEqual(p["nonzero"], 2)
        p = main_matrix_preview(d, "npz", "image", options(value.shape, structure=True, row_range=[301, 310], column_range=[451, 460]))["matrix"]["plane"]
        self.assertEqual(p["values"][0][0], 1)
        self.assertEqual(p["values"][8][8], 1)

    def test_main_complex_components_and_nonfinite(self):
        v = np.array([[1+2j, np.nan], [3j, np.inf]])
        d = fixture(v)
        for component, transform in (("real", np.real), ("imaginary", np.imag), ("magnitude", np.abs), ("phase", np.angle)):
            with self.subTest(component=component):
                p = main_matrix_preview(d, "npy", "image", options(v.shape, component=component))["matrix"]["plane"]
                expected = transform(v)
                self.assertAlmostEqual(p["values"][0][0], expected[0][0])
        self.assertEqual(main_matrix_preview(d, "npy", "image", options(v.shape))["matrix"]["plane"]["non_finite"], 2)

    def test_region_profiles_and_max512(self):
        v = np.arange(600*700).reshape(600,700); d = fixture(v)
        p = main_matrix_preview(d, "npy", "image", options(v.shape, row_range=[301,310], column_range=[451,460]))["matrix"]["plane"]
        expected = v[301:310,451:460]
        np.testing.assert_array_equal(p["values"], expected)
        np.testing.assert_allclose(p["row_profile"], expected.mean(axis=1))
        np.testing.assert_allclose(p["column_profile"], expected.mean(axis=0))
        self.assertFalse(p["sampled"])
        v = np.ones((512,512)); p = main_matrix_preview(fixture(v), "npy", "image", options(v.shape,max_points=512))
        self.assertEqual(p["matrix"]["plane"]["count"],262144)

    def test_scalar_vector_mat4(self):
        for v in (np.array(42),np.arange(6)):
            p = main_matrix_preview(fixture(v), "npy", "image", options(v.shape))
            self.assertEqual(p["matrix"]["plane"]["values"],v.reshape(1,-1).tolist())
        v = np.arange(12).reshape(3,4)
        p = main_matrix_preview(fixture({"matrix":v},"mat",format="4"),"mat","image",options(v.shape))
        self.assertEqual(p["matrix"]["plane"]["values"],v.tolist())

    def test_result_curves_explicit_and_stable_large_energy(self):
        d=fixture({"S":np.array([4.,3.]),"eigenvalues":np.array([1+2j,1-2j]),"residual_history":np.array([1.,.1,.001])},"npz")
        self.assertEqual(main_matrix_preview(d,"npz")["matrix"]["curves"],[])
        c=main_matrix_preview(d,"npz","series")["matrix"]["curves"]
        self.assertEqual(len(c),4)
        np.testing.assert_allclose(next(x for x in c if "累计" in x["title"])["y"],[.64,1])
        self.assertEqual(next(x for x in c if x["kind"]=="scatter")["y"],[2,-2])
        d=fixture({"S":np.array([4e200,3e200])},"npz")
        np.testing.assert_allclose(main_matrix_preview(d,"npz","series")["matrix"]["curves"][1]["y"],[.64,1])

    def test_mtx_symmetric_hermitian_and_dense(self):
        for value in (sparse.eye(10),np.arange(12).reshape(3,4),sparse.coo_matrix(np.array([[1,2j],[-2j,3]]))):
            d=fixture(value,"mtx")
            p=main_matrix_preview(d,"mtx","image",options(value.shape,component="imaginary"))
            np.testing.assert_array_equal(p["matrix"]["plane"]["values"],(value.toarray() if sparse.issparse(value) else value).imag)

    def test_compressed_classic_mat(self):
        value=np.arange(120).reshape(2,3,4,5)
        d=fixture({"tensor":value,"other":np.ones((4,4))},"mat",do_compression=True)
        self.assertEqual(len(main_matrix_preview(d,"mat")["matrix"]["arrays"]),2)

    def test_object_empty_and_unsafe_integers_rejected(self):
        for value in (np.array([{"a":1}],dtype=object),np.empty((0,3))):
            with self.assertRaises(ValueError):main_matrix_preview(fixture(value),"npy")
        with self.assertRaises(ValueError):main_matrix_preview(fixture(np.array([2**60],dtype=np.int64)),"npy","image",options([1]))

    def test_request_payload_binding_mutations(self):
        value=np.arange(12).reshape(3,4); d=fixture(value); o=options(value.shape)
        result=main_matrix_preview(d,"npy","image",o)
        for mutate in (lambda v:v["selected"].update(component="phase"),lambda v:v["metadata"].update(source_bytes=1),lambda v:v["matrix"]["plane"]["rows"].append(3),lambda v:v["matrix"]["plane"]["values"][0].append(1),lambda v:v["matrix"]["arrays"][0].update(name="/Users/private"),lambda v:v.update(path="/tmp/file"),lambda v:v["matrix"]["plane"].update(sampled=1)):
            bad=copy.deepcopy(result);mutate(bad)
            with self.assertRaises(ValueError):validate_main_matrix_payload(bad,kind="image",options=o,fmt="npy",size=len(d))
        for key,value in (("max_points",True),("structure",1),("axes",[0,0]),("indices",[-1,0]),("row_range",[0,False]),("component","execute")):
            with self.assertRaises(ValueError):validate_main_matrix_options("image",{**o,key:value})
        for kind in ("tree","series"):
            with self.assertRaises(ValueError):validate_main_matrix_options(kind,{"path":"/etc/passwd"})

    def test_zip_paths_duplicates_and_pickle_members(self):
        for names in (("../x.npy",),("x.npy","x.npy")):
            out=io.BytesIO()
            with warnings_suppressed(), zipfile.ZipFile(out,"w") as z:
                for name in names:z.writestr(name,fixture(np.ones(2)))
            with self.assertRaises(ValueError):main_matrix_preview(out.getvalue(),"npz")


def warnings_suppressed():
    import warnings
    context=warnings.catch_warnings();context.__enter__();warnings.simplefilter("ignore")
    class C:
        def __enter__(self):return self
        def __exit__(self,*args):context.__exit__(*args)
    return C()


if __name__ == "__main__": unittest.main()
