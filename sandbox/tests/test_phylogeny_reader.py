"""Official Newick grammar examples and original bounded/malicious fixtures."""
import ast
import copy
import importlib.util
from pathlib import Path
import unittest

from app.services.phylogeny_reader import phylogeny_preview
from app.services.phylogeny_payload import PhylogenyError, validate_phylogeny_options, validate_phylogeny_payload

SOURCE = "((Human:0.1,Chimp:0.2)95:0.3,Mouse:0.8)Root:0.01;"


def read(source=SOURCE, fmt="nwk"):
    return phylogeny_preview(source.encode("utf-8") if isinstance(source, str) else source, fmt)


class PhylogenyTests(unittest.TestCase):
    def test_official_phylip_topology_example(self):
        value = read("(B,(A,C,E),D);")
        self.assertEqual([n["label"] for n in value["phylogeny"]["nodes"]], [None, "B", None, "A", "C", "E", "D"])
        self.assertEqual(value["metadata"]["leaf_count"], 5)
        self.assertEqual(value["metadata"]["max_depth"], 2)
        self.assertEqual(value["metadata"]["missing_lengths"], 6)
        self.assertFalse(value["metadata"]["branch_length_mode"])

    def test_exact_lengths_numeric_internal_label_and_unspecified_rootedness(self):
        value = read()
        self.assertEqual(value["phylogeny"]["nodes"][1]["label"], "95")
        self.assertEqual(value["phylogeny"]["nodes"][1]["length"], "0.3")
        self.assertEqual(value["phylogeny"]["nodes"][0]["length"], "0.01")
        self.assertEqual(value["phylogeny"]["rootedness"], "unspecified")
        self.assertTrue(value["metadata"]["root_length_present"])
        self.assertTrue(value["metadata"]["branch_length_mode"])
        self.assertNotIn("support", str(value))

    def test_quoted_labels_apostrophes_unicode_and_underscore_rule(self):
        value = read("('O''Brien':0.1,Homo_sapiens:0.2,'literal_underscore':0.3,'模式,物种':0.4);")
        self.assertEqual([n["label"] for n in value["phylogeny"]["nodes"]][1:], ["O'Brien", "Homo sapiens", "literal_underscore", "模式,物种"])

    def test_empty_labels_unary_and_polytomy(self):
        for source, count in [("(,(,,),);", 7), ("(((A)));", 4), ("();", 2), ("('',B);", 3)]:
            with self.subTest(source=source): self.assertEqual(read(source)["metadata"]["node_count"], count)

    def test_bom_whitespace_and_extensions(self):
        for fmt in ["nwk", "newick", "tree", "tre"]:
            self.assertEqual(read("\ufeff \n( A : .5 , B : +1.e-2 ) ; \n", fmt)["metadata"]["format"], fmt)

    def test_valid_length_bounds_and_preserved_lexemes(self):
        for raw in ["0", "0.0000", "+.5", "1.", "01.00", "1e-12", "1E+9", "0e999", "0e-999"]:
            self.assertEqual(read(f"(A:{raw},B:1);")["phylogeny"]["nodes"][1]["length"], raw)

    def test_missing_lengths_never_become_zero_and_root_does_not_enable_scale(self):
        for source in ["(A:0,B:0);", "(A:0,B:0):5;", "(A,B:1);", "((A:1,B:1),C:1);"]:
            self.assertFalse(read(source)["metadata"]["branch_length_mode"])
        self.assertIsNone(read("(A,B:1);")["phylogeny"]["nodes"][1]["length"])

    def test_invalid_numbers_rejected_not_coerced(self):
        for raw in ["-0", "-1", "NaN", "inf", "1e309", "1e-999", "1e-13", "1000000001", "1000000000.0000000000001", "9.999999999999999999e-13", "1e1000", "1e", "1e+", ".", "+", "", "1 2", "1_2", "1" * 33]:
            with self.subTest(raw=raw), self.assertRaises(PhylogenyError): read(f"(A:{raw},B:1);")

    def test_multiple_trees_trailing_garbage_and_missing_delimiters(self):
        for raw in ["(A,B);(C,D);", "(A,B); text", "(A,B)", "A;", "(A B);", "(A,,B", "(A,B));", "('missing,B);", "(A:'1',B:2);", "(A,B);\x00"]:
            with self.subTest(raw=raw), self.assertRaises(PhylogenyError): read(raw)

    def test_comments_annotations_nexus_and_xml_not_silently_interpreted(self):
        for raw in ["[&R](A,B);", "(A[&&NHX:S=human],B);", "(A,B)[note];", "(A{Foreground},B);", "#NEXUS\nbegin trees; tree T=(A,B); end;", '<phyloxml><phylogeny><clade/></phylogeny></phyloxml>', '<!DOCTYPE x [<!ENTITY y SYSTEM "file:///etc/passwd">]><x>&y;</x>']:
            with self.subTest(raw=raw), self.assertRaises(PhylogenyError): read(raw)

    def test_text_security_and_budget(self):
        for name in ["<img>", "https://evil.invalid/x", "javascript:alert(1)", "/Users/private", "C:\\Users\\private", "\u202eabc", "bad\x00text", "x" * 257]:
            with self.subTest(name=name), self.assertRaises(PhylogenyError): read(f"('{name}',B);")
        self.assertEqual(read("('" + "科" * 256 + "',B);")["metadata"]["node_count"], 3)

    def test_node_and_depth_boundaries(self):
        self.assertEqual(read("(" + ",".join("A" for _ in range(999)) + ");")["metadata"]["node_count"], 1000)
        with self.assertRaises(PhylogenyError): read("(" + ",".join("A" for _ in range(1000)) + ");")
        self.assertEqual(read("(" * 64 + "A" + ")" * 64 + ";")["metadata"]["max_depth"], 64)
        with self.assertRaises(PhylogenyError): read("(" * 65 + "A" + ")" * 65 + ";")

    def test_input_encoding_format_options_and_source_budgets(self):
        for source in [b"", b"\xff", b" " * (4194304 + 1), bytearray(b"(A,B);")]:
            with self.assertRaises(PhylogenyError): phylogeny_preview(source, "nwk")
        for fmt in ["xml", "nex", "NWK", ["nwk"], None]:
            with self.assertRaises(PhylogenyError): read(fmt=fmt)
        for kind, options in [("tree", []), ("image", {}), ("tree", {"support": True}), (["tree"], {})]:
            with self.assertRaises(PhylogenyError): validate_phylogeny_options(kind, options)

    def test_payload_rejects_typing_semantics_topology_and_injection(self):
        changes = [lambda r: r.update(contract_version=2.0), lambda r: r.update(sampled=True), lambda r: r.update(kind="graph"), lambda r: r["phylogeny"].update(rootedness="rooted"), lambda r: r["phylogeny"].update(url="file:///etc/passwd"), lambda r: r["phylogeny"]["nodes"][1].update(parent="n1"), lambda r: r["phylogeny"]["nodes"][2].update(parent="n999"), lambda r: r["phylogeny"]["nodes"][1].update(id="n2"), lambda r: r["phylogeny"]["nodes"][1].update(length=0.3), lambda r: r["phylogeny"]["nodes"][1].update(support=95), lambda r: r["metadata"].update(max_depth=True), lambda r: r["metadata"].update(branch_length_mode=1), lambda r: r["metadata"].update(missing_lengths=1), lambda r: r["metadata"].update(root_length_present=False), lambda r: r["metadata"].update(format=["nwk"])]
        for change in changes:
            value = read(); change(value)
            with self.subTest(change=change), self.assertRaises(PhylogenyError): validate_phylogeny_payload(value)

    def test_payload_preorder_cannot_reopen_closed_branch(self):
        value = read(); value["phylogeny"]["nodes"].append({"id": "n5", "parent": "n1", "label": "C", "length": "1"})
        with self.assertRaises(PhylogenyError): validate_phylogeny_payload(value)

    def test_payload_source_format_and_effective_output_binding(self):
        value = read()
        for kwargs in [{"size": True}, {"size": len(SOURCE) + 1}, {"fmt": "tree"}, {"fmt": ["nwk"]}, {"limit": True}, {"limit": 100}, {"limit": 1048577}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(PhylogenyError): validate_phylogeny_payload(value, **kwargs)

    def test_failures_do_not_echo_sensitive_source(self):
        token = "/Users/sensitive-patient/secret-file"
        for value in [f"('{token}',B);", f"<!DOCTYPE x SYSTEM 'file://{token}'>", f"(A,B);{token}"]:
            with self.assertRaises(PhylogenyError) as raised: read(value)
            self.assertNotIn(token, str(raised.exception)); self.assertNotIn("sensitive-patient", str(raised.exception))

    def test_backend_sandbox_validator_ast_and_actual_result_parity(self):
        root = Path(__file__).resolve().parents[2]
        backend = root / 'backend/app/application/services/phylogeny_visualization.py'
        worker = root / 'sandbox/app/services/phylogeny_payload.py'
        if not backend.is_file(): self.skipTest('Repository root not mounted')
        self.assertEqual(ast.dump(ast.parse(worker.read_text())), ast.dump(ast.parse(backend.read_text())))
        spec = importlib.util.spec_from_file_location('phylogeny_api_pure', backend); module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        value = read(); self.assertIs(module.validate_phylogeny_payload(value, size=len(SOURCE), fmt="nwk"), value)
        bad = copy.deepcopy(value); bad['phylogeny']['nodes'][1]['length'] = '1e-999'
        with self.assertRaises(ValueError): module.validate_phylogeny_payload(bad)


if __name__ == '__main__': unittest.main()
