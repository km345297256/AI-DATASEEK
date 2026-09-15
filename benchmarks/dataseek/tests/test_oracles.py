"""Oracle checks, including independent source assertions and mutations.

Full-source tests run with the existing sandbox image, DATASEEK_BENCHMARK_DATASETS
pointing at its read-only data mount. Standard-library mutation tests run locally.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import tempfile
import unittest

from benchmarks.dataseek import oracles


class OracleMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "manifest.json").write_text('{"domain":"general"}', encoding="utf-8")

    def test_fasta_ambiguity_and_wrapping_have_explicit_denominators(self):
        path = self.root / "sample.fasta"
        path.write_text(">sample description\na c\nGtNN-\n>second\nGG\n", encoding="ascii")
        tasks = oracles._fasta_tasks("ncbi-lambda-reference", self.root)
        self.assertEqual(tasks[0]["expected"]["answer"]["records"],
                         [{"id": "sample", "length": 7}, {"id": "second", "length": 2}])
        self.assertEqual(tasks[1]["expected"]["answer"],
                         {"A": 1, "C": 1, "G": 3, "T": 1, "other": 3, "gc_fraction": 4 / 6})
        path.write_text(">sample\nACGTNN-\n>second\nGG\n", encoding="ascii")
        self.assertEqual(oracles._fasta_tasks("ncbi-lambda-reference", self.root)[1]["expected"],
                         tasks[1]["expected"])

    def test_real_measurement_mutation_changes_gold_and_input_hash(self):
        path = self.root / "iris.csv"
        path.write_text("species,sepal_length_cm\na,2\na,4\nb,8\n", encoding="ascii")
        before = oracles._csv_tasks("open-uci-iris", self.root)
        path.write_text("species,sepal_length_cm\na,2\na,6\nb,8\n", encoding="ascii")
        after = oracles._csv_tasks("open-uci-iris", self.root)
        self.assertEqual(before[0]["expected"], after[0]["expected"])
        self.assertEqual(before[1]["expected"]["answer"]["groups"][0]["mean"], 3)
        self.assertEqual(after[1]["expected"]["answer"]["groups"][0]["mean"], 4)
        self.assertNotEqual(before[1]["metadata"]["input_identities"],
                            after[1]["metadata"]["input_identities"])

    def test_csv_rejects_ragged_rows_and_respects_quoted_delimiters(self):
        path = self.root / "table.csv"
        path.write_text('"label;kind";value\n"x;y";1\n', encoding="ascii")
        self.assertEqual(oracles._csv_table(path, ";"), (["label;kind", "value"], [["x;y", "1"]]))
        path.write_text("a,b\n1,2,3\n", encoding="ascii")
        with self.assertRaises(ValueError):
            oracles._csv_table(path)

    def test_xrd_uses_declared_grid_and_first_tied_maximum(self):
        path = self.root / "sample.xrdml"
        path.write_text('''<root xmlns="urn:oracle-test"><scan>
            <positions axis="2Theta" unit="deg"><startPosition>10</startPosition>
            <endPosition>40</endPosition></positions><intensities unit="counts">1 9 9 2</intensities>
            </scan></root>''', encoding="ascii")
        task = oracles._xrd_tasks("mendeley-calcium-carbonate", self.root)[1]
        row = task["expected"]["answer"]["files"][0]
        self.assertEqual(row["max_index_zero_based"], 1)
        self.assertEqual(row["angle_at_max_deg"], 20)
        self.assertEqual(row["max_raw_intensity"], 9)

    def test_input_symlink_does_not_cross_dataset_boundary(self):
        outside = self.root / "actual.csv"
        outside.write_text("x\n1\n", encoding="ascii")
        (self.root / "linked.csv").symlink_to(outside)
        with self.assertRaises(ValueError):
            oracles._task("dataset", self.root, 1, "Read", "{}", {}, ["linked.csv"])


class BundledSourceOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dependencies = ("numpy", "PIL", "xlrd", "netCDF4", "astropy", "osgeo", "pypdf")
        missing = [name for name in dependencies if importlib.util.find_spec(name) is None]
        if missing:
            raise unittest.SkipTest("Full oracle checks require the existing scientific sandbox image: " + ", ".join(missing))
        cls.root = Path(os.environ.get("DATASEEK_BENCHMARK_DATASETS",
                                      Path(__file__).resolve().parents[3] / "backend/app/resources/datasets"))
        cls.tasks = oracles.build_gold_tasks(cls.root)
        cls.by_id = {task["id"]: task for task in cls.tasks}

    def answer(self, dataset, kind="analysis"):
        return self.by_id[f"{dataset}--{kind}"]["expected"]["answer"]

    def test_complete_source_catalog_obeys_protocol_and_contains_only_raw_inputs(self):
        from benchmarks.dataseek.protocol import validate_tasks
        validate_tasks(self.tasks)
        self.assertEqual(len(self.tasks), 36)
        self.assertEqual({task["dataset_id"] for task in self.tasks}, set(oracles.DATASET_IDS))
        for task in self.tasks:
            with self.subTest(task=task["id"]):
                self.assertEqual(task["required_artifacts"], [{"name": "answer.json", "kind": "json"}])
                self.assertNotIn("manifest.json", task["input_files"])
                self.assertNotIn("SOURCE.md", task["input_files"])
                self.assertNotIn(str(self.root), json.dumps(task))
                for item in task["metadata"]["input_identities"]:
                    data = (self.root / task["dataset_id"] / item["name"]).read_bytes()
                    self.assertEqual(item["size"], len(data))
                    self.assertEqual(item["sha256"], hashlib.sha256(data).hexdigest())

    def test_bundled_table_cardinality_and_empty_sheets(self):
        iris = self.answer("open-uci-iris", "structure")["tables"][0]
        self.assertEqual(iris["row_count"], 150)
        self.assertEqual(len(iris["columns"]), 5)
        self.assertEqual([row["count"] for row in self.answer("open-uci-iris")["groups"]], [50, 50, 50])
        sheets = self.answer("open-uci-concrete", "structure")["sheets"]
        self.assertEqual([(row["row_count"], row["column_count"]) for row in sheets], [(1031, 9), (0, 0), (0, 0)])
        self.assertEqual(self.answer("open-uci-concrete")["data_row_count"], 1030)
        self.assertEqual(sum(row["count"] for row in self.answer("open-uci-wine")["groups"]), 178)

    def test_fasta_base_totals_match_independent_text_length(self):
        for dataset in ("ncbi-lambda-reference", "ncbi-arabidopsis-chloroplast"):
            path = next((self.root / dataset).glob("*.fasta"))
            observed_length = sum(len("".join(line.split())) for line in path.read_text().splitlines()
                                  if not line.startswith(">"))
            answer = self.answer(dataset)
            self.assertEqual(sum(answer[base] for base in ("A", "C", "G", "T", "other")), observed_length)

    def test_fits_fos_zero_is_not_accepted_at_scientific_scale(self):
        from benchmarks.dataseek.scoring import verify_answer
        task = self.by_id["nasa-hst-fos--analysis"]
        wrong = copy.deepcopy(task["expected"])
        wrong["answer"]["mean"] = 0.0
        self.assertFalse(verify_answer(task["expected"], wrong, task["tolerance"])["passed"])
        from astropy.io import fits
        with fits.open(next((self.root / "nasa-hst-fos").glob("*.fits")), memmap=False) as source:
            finite = [float(v) for v in source[0].data.flat if math.isfinite(float(v))]
        self.assertAlmostEqual(task["expected"]["answer"]["mean"] / (math.fsum(finite) / len(finite)), 1.0, places=14)

    def test_netcdf_manual_raw_decode_matches_monthly_oracle(self):
        import numpy as np
        from netCDF4 import Dataset
        with Dataset(str(next((self.root / "open-noaa-air-climatology").glob("*.nc")))) as source:
            variable = source["air"]
            variable.set_auto_maskandscale(False)
            raw = np.asarray(variable[:])
            mask = np.zeros(raw.shape, dtype=bool)
            for name in ("_FillValue", "missing_value"):
                if hasattr(variable, name):
                    for value in np.atleast_1d(getattr(variable, name)):
                        mask |= raw == value
            decoded = raw.astype(np.float64) * float(getattr(variable, "scale_factor", 1)) + float(getattr(variable, "add_offset", 0))
            mask |= ~np.isfinite(decoded)
            means = [math.fsum(float(v) for v in decoded[index][~mask[index]]) / int((~mask[index]).sum())
                     for index in range(raw.shape[0])]
        answer = self.answer("open-noaa-air-climatology")
        np.testing.assert_allclose(answer["monthly_spatial_mean"], means, rtol=1e-12, atol=1e-12)
        self.assertEqual(answer["valid_value_count"], int((~mask).sum()))

    def test_image_mean_uses_full_pixels_and_excludes_only_alpha(self):
        from PIL import Image, ImageStat
        for row in self.answer("bbbc039-nuclei-preview")["files"]:
            with Image.open(self.root / "bbbc039-nuclei-preview" / row["filename"]) as source:
                # Pillow histogram accumulation is independent of numpy reduction.
                means = ImageStat.Stat(source.convert("RGB")).mean
            for actual, reference in zip(row["mean_rgb"], means):
                self.assertAlmostEqual(actual, reference, places=12)

    def test_pdf_rule_page_evidence_and_shapefile_partition(self):
        reproducible = self.answer("plos-reproducible-research")["rule_start_pages"]
        self.assertEqual([row["page"] for row in reproducible], [2] * 6 + [3] * 4)
        notebooks = self.answer("plos-jupyter-notebooks")["rule_start_pages"]
        self.assertEqual([row["page"] for row in notebooks], [2, 3, 3, 4, 4, 4, 5, 5, 6, 6])
        groups = self.answer("open-natural-earth-countries")["groups"]
        self.assertEqual(sum(group["feature_count"] for group in groups),
                         self.answer("open-natural-earth-countries", "structure")["feature_count"])


if __name__ == "__main__":
    unittest.main()
