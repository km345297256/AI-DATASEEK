"""Synthetic graphs plus the topology shape of the GEXF official basic example."""
import ast
import copy
import json
from pathlib import Path
import unittest

from app.services.scientific_graph_reader import scientific_graph_preview
from app.services.scientific_graph_payload import ScientificGraphError, validate_scientific_graph_options, validate_scientific_graph_payload


def graph():
    return {"directed": True, "nodes": [{"id": "TP53", "label": "肿瘤蛋白 / TP53".replace(" / ", " · "), "group": "regulator"}, {"id": "MDM2"}, {"id": "CDKN1A"}], "edges": [{"id": "binding", "source": "TP53", "target": "MDM2", "weight": 0.75, "label": "interaction"}, {"source": "MDM2", "target": "TP53"}]}


def read(value=None):
    return scientific_graph_preview(json.dumps(graph() if value is None else value).encode(), "json")


def graphml(contents='<node id="A"/><node id="B"/><edge source="A" target="B"/>', keys="", directed="undirected"):
    return f'<graphml xmlns="http://graphml.graphdrawing.org/xmlns">{keys}<graph edgedefault="{directed}">{contents}</graph></graphml>'.encode()


def gexf(contents='<nodes><node id="0" label="Hello"/><node id="1" label="World"/></nodes><edges><edge source="0" target="1"/></edges>', attrs='mode="static" defaultedgetype="directed"', version="1.3"):
    namespace = "http://gexf.net/1.3" if version == "1.3" else "http://www.gexf.net/1.2draft"
    return f'<?xml version="1.0" encoding="UTF-8"?><gexf xmlns="{namespace}" version="{version}"><graph {attrs}>{contents}</graph></gexf>'.encode()


class ScientificGraphTests(unittest.TestCase):
    def test_normalized_json_retains_semantics_and_opaque_renderer_ids(self):
        result = read()
        self.assertEqual(result["graph"]["nodes"][0], {"id": "n0", "key": "TP53", "label": "肿瘤蛋白 · TP53", "group": "regulator"})
        self.assertEqual(result["graph"]["edges"][0]["weight"], 0.75)
        self.assertEqual(result["graph"]["edges"][1]["source"], "n1")
        self.assertFalse(result["sampled"])

    def test_static_simple_topology_not_acyclic_tree(self):
        value = graph(); value["edges"] = [{"source": "TP53", "target": "MDM2"}, {"source": "MDM2", "target": "CDKN1A"}, {"source": "CDKN1A", "target": "TP53"}]
        self.assertEqual(len(read(value)["graph"]["edges"]), 3)

    def test_nodes_without_edges_are_valid(self):
        value = graph(); value["edges"] = []
        self.assertEqual(read(value)["metadata"]["edge_count"], 0)

    def test_bad_json_structure_and_nonfinite_numbers(self):
        for data in [b'[]', b'{"directed":true,"directed":false}', b'[' * 10, b'\xff', b'{"directed":true,"nodes":[],"edges":[]}', b'{"directed":true,"nodes":[{"id":"A"}],"edges":[{"source":"A","target":"A","weight":NaN}]}']:
            with self.subTest(data=data), self.assertRaises(ValueError): scientific_graph_preview(data, "json")

    def test_raw_contract_rejects_styles_resources_and_unknown_fields(self):
        mutations = [lambda v: v.update(style={}), lambda v: v["nodes"][0].update(position={"x": 0, "y": 0}), lambda v: v["nodes"][0].update(parent="MDM2"), lambda v: v["edges"][0].update(url="https://example.com"), lambda v: v.update(directed="true"), lambda v: v["nodes"].append(v["nodes"][0])]
        for mutate in mutations:
            value = graph(); mutate(value)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError): read(value)

    def test_label_html_url_control_and_host_path_rejected(self):
        for label in ["<img src=x>", "https://example.com/icon", "data:image/png;base64,abc", "javascript:alert(1)", "/Users/private", "file:/etc/passwd", "a\x00b", "a\u202eb", "x" * 257, ""]:
            value = graph(); value["nodes"][0]["label"] = label
            with self.subTest(label=label), self.assertRaises(ValueError): read(value)

    def test_endpoint_self_loop_duplicate_edges_and_edge_ids_refused(self):
        for edges in [[{"source": "missing", "target": "MDM2"}], [{"source": "TP53", "target": "TP53"}], [{"source": "TP53", "target": "MDM2"}] * 2, [{"id": "same", "source": "TP53", "target": "MDM2"}, {"id": "same", "source": "MDM2", "target": "CDKN1A"}]]:
            value = graph(); value["edges"] = edges
            with self.subTest(edges=edges), self.assertRaises(ValueError): read(value)

    def test_undirected_reverse_edge_is_duplicate(self):
        value = graph(); value["directed"] = False
        with self.assertRaises(ValueError): read(value)

    def test_weight_types_and_budget(self):
        for weight in [True, "1", [], {}, 1e13, 10**999, float("inf")]:
            value = graph(); value["edges"][0]["weight"] = weight
            with self.subTest(weight=weight), self.assertRaises(ValueError): read(value)
        for weight in [-1e12, 0, 1e12, None]:
            value = graph(); value["edges"][0]["weight"] = weight
            self.assertEqual(read(value)["graph"]["edges"][0]["weight"], weight)

    def test_source_and_node_and_edge_count_hard_limits(self):
        with self.assertRaises(ValueError): scientific_graph_preview(b' ' * (4 * 1024**2 + 1), "json")
        value = {"directed": True, "nodes": [{"id": f"n{i}"} for i in range(1000)], "edges": [{"source": f"n{i // 999}", "target": f"n{(i // 999 + i % 999 + 1) % 1000}"} for i in range(3000)]}
        self.assertEqual(read(value)["metadata"]["edge_count"], 3000)
        value["edges"].append({"source": "n3", "target": "n5"})
        with self.assertRaises(ValueError): read(value)
        value["edges"] = []; value["nodes"].append({"id": "n1000"})
        with self.assertRaises(ValueError): read(value)

    def test_graphml_labels_groups_weight_and_defaults(self):
        keys = '<key id="l" for="node" attr.name="label" attr.type="string"/><key id="g" for="node" attr.name="group" attr.type="string"><default>protein</default></key><key id="w" for="edge" attr.name="weight" attr.type="double"><default>2.5</default></key>'
        result = scientific_graph_preview(graphml('<node id="A"><data key="l">Gene A</data></node><node id="B"/><edge source="A" target="B"/>', keys), "graphml")
        self.assertEqual(result["graph"]["nodes"][0]["label"], "Gene A")
        self.assertEqual(result["graph"]["nodes"][1]["group"], "protein")
        self.assertEqual(result["graph"]["edges"][0]["weight"], 2.5)

    def test_graphml_rejects_unsupported_extensions_and_scopes(self):
        for contents, keys in [('<node id="A"><graph edgedefault="directed"/></node>', ''), ('<node id="A"/><hyperedge/>', ''), ('<node id="A" xlink:href="https://x"/>', ''), ('<node id="A"><data key="unknown">a</data></node>', ''), ('<node id="A"/>', '<key id="a" for="node" attr.name="url" attr.type="string"/>'), ('<node id="A"><data key="w">1</data></node>', '<key id="w" for="edge" attr.name="weight" attr.type="double"/>')]:
            with self.subTest(contents=contents), self.assertRaises(ValueError): scientific_graph_preview(graphml(contents, keys), "graphml")

    def test_graphml_duplicate_data_and_conflicting_direction_fail(self):
        keys = '<key id="a" for="node" attr.name="label" attr.type="string"/>'
        for value in [graphml('<node id="A"><data key="a">A</data><data key="a">B</data></node>', keys), graphml('<node id="A"/><node id="B"/><edge source="A" target="B" directed="true"/>')]:
            with self.assertRaises(ValueError): scientific_graph_preview(value, "graphml")

    def test_xml_dtd_entities_pi_cdata_and_depth_rejected(self):
        for value in [b'<!DOCTYPE graphml [<!ENTITY x SYSTEM "file:///etc/passwd">]>' + graphml(), b'<?custom a="b"?>' + graphml(), graphml('<node id="A"><![CDATA[text]]></node>'), graphml('<node id="A">' * 8 + '</node>' * 8)]:
            with self.subTest(value=value), self.assertRaises(ValueError): scientific_graph_preview(value, "graphml")

    def test_xml_entity_encoded_html_is_rejected_after_decoding(self):
        keys = '<key id="a" for="node" attr.name="label" attr.type="string"/>'
        with self.assertRaises(ValueError): scientific_graph_preview(graphml('<node id="A"><data key="a">&lt;script&gt;x&lt;/script&gt;</data></node>', keys), "graphml")

    def test_gexf_official_basic_topology_both_versions(self):
        for version in ("1.2", "1.3"):
            result = scientific_graph_preview(gexf(version=version), "gexf")
            self.assertEqual(result["metadata"]["dialect"], f"gexf-{version}-static")
            self.assertEqual(result["graph"]["nodes"][0]["label"], "Hello")

    def test_gexf_native_labels_and_weights(self):
        result = scientific_graph_preview(gexf('<nodes><node id="a"/><node id="b"/></nodes><edges><edge id="ab" source="a" target="b" label="binding" weight="0.125"/></edges>'), "gexf")
        self.assertEqual(result["graph"]["edges"][0]["weight"], 0.125)

    def test_gexf_dynamic_hierarchy_attvalues_viz_and_bad_namespace_rejected(self):
        for value in [gexf(attrs='mode="dynamic" defaultedgetype="directed"'), gexf('<nodes><node id="a"><nodes/></node></nodes>'), gexf('<nodes><node id="a"><attvalues/></node></nodes>'), gexf('<nodes><node id="a"><color r="1"/></node></nodes>'), gexf().replace(b'http://gexf.net/1.3', b'https://example.com/other'), gexf(attrs='defaultedgetype="mutual"')]:
            with self.subTest(value=value), self.assertRaises(ValueError): scientific_graph_preview(value, "gexf")

    def test_format_mismatch_refuses_wrong_parser(self):
        for fmt, data in [('graphml', gexf()), ('gexf', graphml()), ('json', gexf()), ('csv', b'a,b')]:
            with self.assertRaises(ValueError): scientific_graph_preview(data, fmt)

    def test_options_are_empty_and_strict(self):
        self.assertEqual(validate_scientific_graph_options("graph", {}), {})
        for kind, options in [('tree', {}), ('graph', []), ('graph', {'layout': 'cose'}), (['graph'], {})]:
            with self.assertRaises(ValueError): validate_scientific_graph_options(kind, options)

    def test_validator_rejects_schema_tampering(self):
        changes = [lambda r: r.update(contract_version=2.0), lambda r: r.update(reader='other'), lambda r: r.update(sampled=True), lambda r: r['metadata'].update(node_count=True), lambda r: r['metadata'].update(simple=1), lambda r: r['metadata'].update(format=['json']), lambda r: r['metadata'].update(dialect=['dataseek-graph-json-v1']), lambda r: r['graph']['nodes'][0].update(id='n9'), lambda r: r['graph']['edges'][0].update(source='missing'), lambda r: r.update(url='https://example.com')]
        for change in changes:
            result = read(); change(result)
            with self.subTest(change=change), self.assertRaises(ValueError): validate_scientific_graph_payload(result)

    def test_source_binding_and_effective_output_budget(self):
        result = read()
        self.assertIs(validate_scientific_graph_payload(result, size=result['metadata']['source_bytes']), result)
        for kwargs in [{'size': result['metadata']['source_bytes'] + 1}, {'size': True}, {'limit': 100}, {'limit': True}]:
            with self.assertRaises(ValueError): validate_scientific_graph_payload(result, **kwargs)

    def test_error_messages_never_echo_source_values_or_paths(self):
        token = '/Users/private-patient-secret/record'
        cases = [(json.dumps({'directed': True, 'nodes': [{'id': token}], 'edges': []}).encode(), 'json'), (graphml(f'<node id="A"><data key="{token}">secret</data></node>'), 'graphml'), (f'<!DOCTYPE gexf SYSTEM "file://{token}">'.encode() + gexf(), 'gexf'), (f'<broken attribute="{token}" '.encode(), 'graphml')]
        for data, fmt in cases:
            with self.subTest(fmt=fmt), self.assertRaises(ValueError) as error: scientific_graph_preview(data, fmt)
            self.assertNotIn(token, str(error.exception)); self.assertNotIn('private-patient-secret', str(error.exception))

    def test_backend_and_sandbox_pure_validator_sources_are_identical(self):
        root = Path(__file__).resolve().parents[1]
        backend = root.parent / 'backend/app/application/services/scientific_graph_visualization.py'
        if not backend.is_file(): self.skipTest('Backend source not mounted in isolated sandbox-only context')
        worker = root / 'app/services/scientific_graph_payload.py'
        self.assertEqual(ast.dump(ast.parse(worker.read_text())), ast.dump(ast.parse(backend.read_text())))


if __name__ == '__main__': unittest.main()
