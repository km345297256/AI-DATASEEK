"""Host-side matrix contract has no NumPy, filesystem or scientific parsing."""
import copy
import importlib.util
import pathlib
import unittest

from app.application.services.main_matrix_visualization import (LIMITS, WARNING,
    validate_main_matrix_options, validate_main_matrix_payload)


def payload(kind="tree"):
    selection={"variable":"array-0","axes":[0,1],"indices":[0,0],"component":"real","row_range":None,"column_range":None,"max_points":256,"structure":False}
    plane={"values":[[1,3]],"rows":[0],"columns":[0,1],"shape":[1,2],"sampled":False,"row_step":1,"column_step":1,"row_range":[0,1],"column_range":[0,2],"structure":False,"nonzero":2,"finite_count":2,"count":2,"minimum":1,"maximum":3,"mean":2,"standard_deviation":1,"row_profile":[2],"column_profile":[1,3],"non_finite":0}
    return {"contract_version":2,"type":"matrix-workbench","reader":"matrix-workbench","kind":kind,"media_type":"application/json","selected":selection if kind=="image" else {},"matrix":{"arrays":[{"id":"array-0","name":"x","shape":[1,2],"dtype":"float64","sparse":False}],"plane":plane if kind=="image" else None,"curves":[]},"metadata":{"format":"npy","input_mode":"whole","source_bytes":144,"limits":dict(LIMITS),"matrix_semantics":"selected component; sampled statistics; sparse structure bins nonzero entries"},"warnings":[WARNING],"sampled":False}


class MatrixHostContractTests(unittest.TestCase):
    def test_three_kinds_and_bindings(self):
        for kind in ("tree","image","series"):
            with self.subTest(kind=kind):
                p=payload(kind)
                self.assertIs(validate_main_matrix_payload(p,kind=kind,options=p["selected"],fmt="npy",size=144),p)
                for bad in ({"kind":"report"},{"fmt":"mat"},{"size":145},{"limit":64},{"options":{"path":"/secret"}}):
                    with self.assertRaises(ValueError):validate_main_matrix_payload(p,**bad)

    def test_invalid_structures_fail_closed(self):
        mutations=[lambda p:p.update(contract_version=True),lambda p:p.update(reader="h5web"),lambda p:p.update(kind="geometry"),lambda p:p.update(sampled=True),lambda p:p.update(path="/private/path"),lambda p:p["metadata"].update(limits={**LIMITS,"max_values":512*513}),lambda p:p["matrix"]["arrays"][0].update(name="<script>"),lambda p:p["matrix"]["arrays"][0].update(name="/Users/private"),lambda p:p["matrix"]["arrays"][0].update(shape=[True,2]),lambda p:p["matrix"]["arrays"][0].update(dtype="object"),lambda p:p["matrix"]["plane"]["values"][0].append(3),lambda p:p["matrix"]["plane"].update(row_step=True),lambda p:p["matrix"]["plane"].update(mean=float("nan")),lambda p:p["matrix"]["plane"].update(standard_deviation=-1),lambda p:p["matrix"]["plane"].update(non_finite=1),lambda p:p["matrix"]["plane"].update(minimum=0),lambda p:p["matrix"]["plane"].update(row_range=[1,2]),lambda p:p["matrix"]["plane"].update(row_profile=[1,3])]
        for i,mutate in enumerate(mutations):
            with self.subTest(case=i):
                p=payload("image");mutate(p)
                with self.assertRaises(ValueError):validate_main_matrix_payload(p)

    def test_options_strict_types_and_unknown_keys(self):
        o=payload("image")["selected"]
        for update in ({"variable":"/tmp/file"},{"variable":"array-00"},{"axes":[True,1]},{"indices":[False,0]},{"max_points":512.0},{"structure":1},{"row_range":[False,1]},{"component":"abs"},{"path":"/tmp/file"}):
            with self.subTest(update=update):
                with self.assertRaises(ValueError):validate_main_matrix_options("image",{**o,**update})

    def test_cross_build_pure_contract_copy(self):
        host=pathlib.Path(__file__).parents[1]/"app/application/services/main_matrix_visualization.py"
        sandbox=pathlib.Path(__file__).parents[2]/"sandbox/app/services/main_matrix_payload.py"
        if sandbox.exists():self.assertEqual(host.read_bytes(),sandbox.read_bytes())
        spec=importlib.util.spec_from_file_location("isolated_matrix_contract",host)
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        self.assertEqual(m.validate_main_matrix_payload(payload("image")),payload("image"))


if __name__=="__main__":unittest.main()
