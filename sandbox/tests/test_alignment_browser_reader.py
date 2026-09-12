"""Native pysam oracle and offline CRAM verification for the Cordis migration."""
import copy
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import pysam

from app.services.alignment_browser_reader import alignment_browser_preview
from app.services.alignment_browser_payload import validate_options,validate_payload
from tests.alignment_browser_fixtures import OPTIONS,HEADER,encoded,records,sam_bytes


class AlignmentBrowserNativeTests(unittest.TestCase):
    def test_sam_bam_native_coverage_and_cigar_events(self):
        for fmt in ("sam","bam"):
            with self.subTest(fmt=fmt),encoded(fmt) as (data,_,path):
                tree=alignment_browser_preview(data,fmt)
                self.assertEqual(tree["choices"]["references"],[{"id":0,"name":"chr1","length":5000}])
                self.assertNotIn("hidden sample",json.dumps(tree));self.assertNotIn("/Users/private",json.dumps(tree));self.assertNotIn("https:",json.dumps(tree))
                result=alignment_browser_preview(data,fmt,"table",OPTIONS);a=result["alignment"]
                self.assertTrue(a["scan_complete"]);self.assertEqual(a["matched_reads"],5)
                with pysam.AlignmentFile(str(path),"r" if fmt=="sam" else "rb") as handle:
                    oracle=sum(sum(OPTIONS["start"]<=p<OPTIONS["end"] for p in r.get_reference_positions()) for r in handle.fetch(until_eof=True))
                self.assertEqual(sum(x["covered_bases"] for x in a["coverage"]),oracle)
                self.assertEqual(oracle,38)
                first=a["reads"][0]
                self.assertEqual(first["blocks"],[{"start":100,"end":105},{"start":105,"end":108},{"start":110,"end":114},{"start":119,"end":122}])
                self.assertEqual(first["insertions"],[{"position":105,"length":2,"sequence":"AA"}])
                self.assertEqual(first["deletions"],[{"start":108,"end":110,"length":2}])
                self.assertEqual(first["splices"],[{"start":114,"end":119,"length":5}])
                self.assertEqual(first["mismatches"],[{"position":102,"query":"G","reference":"C"}])
                self.assertEqual(a["reads"][4]["mismatches"],[{"position":144,"query":"C","reference":"A"}])
                self.assertFalse(a["reads"][3]["mismatch_available"])

    def test_cram_no_reference_and_embedded_decode_offline(self):
        before={key:os.environ.get(key) for key in ("REF_PATH","REF_CACHE")}
        for mode in ("no_ref","embedded"):
            with self.subTest(mode=mode),encoded("cram",mode) as (data,reference,path):
                self.assertTrue(reference.exists())
                tree=alignment_browser_preview(data,"cram")
                result=alignment_browser_preview(data,"cram","table",OPTIONS)
                self.assertEqual(result["alignment"]["matched_reads"],5)
                self.assertEqual(sum(p["covered_bases"] for p in result["alignment"]["coverage"]),38)
                self.assertNotIn(str(reference),json.dumps(tree)+json.dumps(result))
                self.assertEqual(result["alignment"]["reads"][0]["mismatches"],[{"position":102,"query":"G","reference":"C"}])
        self.assertEqual(before,{key:os.environ.get(key) for key in before})

    def test_cram_external_reference_rejected_even_when_valid_reference_still_exists(self):
        with encoded("cram","external") as (data,reference,path):
            # Positive oracle: the exact same source decodes with the actual reference.
            with pysam.AlignmentFile(str(path),"rc",reference_filename=str(reference)) as f:self.assertEqual(len(list(f.fetch(until_eof=True))),5)
            with self.assertRaisesRegex(ValueError,"离线|参考"):
                alignment_browser_preview(data,"cram","table",OPTIONS)
            self.assertEqual(alignment_browser_preview(data,"cram")["kind"],"tree")
            code="import sys;from app.services.alignment_browser_reader import alignment_browser_preview;from tests.alignment_browser_fixtures import OPTIONS\ntry:alignment_browser_preview(sys.stdin.buffer.read(),'cram','table',OPTIONS)\nexcept ValueError:sys.exit(19)\nsys.exit(0)"
            child=subprocess.run([sys.executable,"-c",code],input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20)
            self.assertEqual(child.returncode,19)
            self.assertEqual(child.stdout,b"")

    def test_display_and_matched_scan_budget_are_reported(self):
        with encoded() as (data,_,__):
            result=alignment_browser_preview(data,"sam","table",{**OPTIONS,"max_reads":1})
            self.assertEqual(len(result["alignment"]["reads"]),1)
            self.assertEqual(result["alignment"]["matched_reads"],4)
            self.assertFalse(result["alignment"]["scan_complete"])
            self.assertTrue(result["alignment"]["reads_truncated"])
            self.assertTrue(result["sampled"])

    def test_record_budget_includes_unmatched_unmapped_records(self):
        header=pysam.AlignmentHeader.from_dict(HEADER)
        r=pysam.AlignedSegment(header);r.query_name="unmapped";r.flag=4;r.query_sequence="A"
        with encoded(input_records=[r]*100001) as (data,_,__):
            result=alignment_browser_preview(data,"sam","table",OPTIONS)
            a=result["alignment"];self.assertEqual(a["records_scanned"],100000);self.assertEqual(a["matched_reads"],0);self.assertFalse(a["scan_complete"]);self.assertTrue(result["sampled"])

    def test_long_query_budget_does_not_claim_complete_coverage(self):
        h={**HEADER,"SQ":[{"SN":"chr1","LN":300000}]};header=pysam.AlignmentHeader.from_dict(h)
        r=pysam.AlignedSegment(header);r.query_name="long";r.reference_id=0;r.reference_start=0;r.mapping_quality=60;r.cigarstring="200000M";r.query_sequence="A"*200000
        with encoded(input_records=[r]*21,header=h) as (data,_,__):
            p=alignment_browser_preview(data,"sam","table",{**OPTIONS,"start":0,"end":200000,"max_reads":2000})
            self.assertEqual(p["alignment"]["matched_reads"],20);self.assertFalse(p["alignment"]["scan_complete"])
            self.assertEqual(sum(b["covered_bases"] for b in p["alignment"]["coverage"]),4000000)

    def test_strict_options_and_private_output_binding(self):
        source=sam_bytes();v=alignment_browser_preview(source,"sam","table",OPTIONS)
        for changes in ({"reference":True},{"start":-1},{"end":OPTIONS["start"]},{"max_reads":0},{"bins":19},{"path":"/private/reference.fa"}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):validate_options("table",{**OPTIONS,**changes})
        for changes in ({"source_bytes":1},{"format":"bam"},{"kind":"tree"},{"options":{**OPTIONS,"end":161}}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):validate_payload(v,**changes)
        for mutate in (lambda p:p["alignment"]["coverage"][0].update(depth=999),lambda p:p["alignment"]["reads"][0].update(name="/Users/private"),lambda p:p["alignment"]["reads"][0].update(flag=0),lambda p:p.update(url="https://external")):
            bad=copy.deepcopy(v);mutate(bad)
            with self.assertRaises(ValueError):validate_payload(bad)

    def test_header_path_metadata_never_reaches_payload(self):
        h={**HEADER,"SQ":[{"SN":"chr1","LN":5000,"UR":"file:///Users/private/ref.fa"}],"RG":[{"ID":"rg1","SM":"/Users/private/sample","LB":"secret"}],"PG":[{"ID":"pipeline","CL":"process /Users/private/dataset.bam"}]}
        with encoded(header=h) as (data,_,__):
            p=alignment_browser_preview(data,"sam","table",OPTIONS)
            self.assertNotIn("/Users/private",json.dumps(p));self.assertNotIn("file://",json.dumps(p));self.assertNotIn("secret",json.dumps(p))

    def test_long_introns_use_run_walker_without_expanding_reference_gaps(self):
        h={**HEADER,"SQ":[{"SN":"chr1","LN":20000000}]};header=pysam.AlignmentHeader.from_dict(h)
        r=pysam.AlignedSegment(header);r.query_name="spliced";r.reference_id=0;r.reference_start=100;r.mapping_quality=60;r.cigarstring="1M"+"2000000N"*8+"1M";r.query_sequence="AC";r.set_tag("MD","1A0")
        with encoded(input_records=[r],header=h) as (data,_,__):
            value=alignment_browser_preview(data,"sam","table",OPTIONS)
            read=value["alignment"]["reads"][0]
            self.assertEqual(read["mismatches"],[{"position":16000101,"query":"C","reference":"A"}])
            self.assertEqual(len(read["splices"]),8)
            self.assertEqual(sum(b["covered_bases"] for b in value["alignment"]["coverage"]),1)

    def test_md_invalid_lengths_and_missing_deletion_are_rejected(self):
        for md in ("999999999", "2C5AA7", "2C5^A7"):
            h,rs=records();rs[0].set_tag("MD",md)
            with self.subTest(md=md),encoded(input_records=rs) as (data,_,__):
                with self.assertRaises(ValueError):alignment_browser_preview(data,"sam","table",OPTIONS)

    def test_cram_reheader_preserves_m5_and_does_not_emit_binary_stdout(self):
        from app.services.alignment_browser_reader import _offline_cram
        from pathlib import Path
        import tempfile
        with encoded("cram","embedded") as (source,reference,path), tempfile.TemporaryDirectory(prefix="alignment-header-test-") as d:
            dummy=Path(d)/"dummy.fa";dummy.write_text(">disabled\nN\n")
            safe=_offline_cram(path,d,dummy)
            with pysam.AlignmentFile(str(path),"rc") as original,pysam.AlignmentFile(str(safe),"rc") as rewritten:
                a,b=original.header.to_dict(),rewritten.header.to_dict()
                self.assertEqual(a["SQ"][0]["M5"],b["SQ"][0]["M5"])
                self.assertNotIn("UR",b["SQ"][0])
                self.assertEqual(a["SQ"][0]["SN"],b["SQ"][0]["SN"])
                self.assertEqual(len(list(rewritten.fetch(until_eof=True))),5)

    def test_cram_complete_worker_stdout_is_single_json_envelope(self):
        for mode in ("no_ref","embedded","external"):
            with self.subTest(mode=mode),encoded("cram",mode) as (source,_,__):
                header={"contract_version":2,"size":len(source),"reader":"alignment-browser","kind":"table","format":"cram","options":OPTIONS,"truncated":False}
                process=subprocess.run([sys.executable,"-m","app.services.extended_visualization_worker"],input=json.dumps(header).encode()+b"\n"+source,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=25)
                self.assertEqual(process.returncode,0)
                result=json.loads(process.stdout)
                self.assertEqual(result["ok"],mode!="external")
                if result["ok"]:self.assertEqual(result["data"]["alignment"]["matched_reads"],5)
                self.assertNotIn(b"CRAM\x03",process.stdout)


if __name__=="__main__":unittest.main()
