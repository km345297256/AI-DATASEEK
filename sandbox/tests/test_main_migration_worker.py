"""Real registered-worker dispatch and stdin tests for all six main readers.

Uses only existing synthetic producers. No source/parser dispatch is mocked in
successful checks; the same preview_bytes/_encoded_result entry points run in
the isolated production worker. Production code and services are untouched.
"""
import copy
import gzip
import io
import json
import numpy as np
import pytest

from app.services import extended_visualization_worker as worker
from app.services import main_migration_reader as migration
from tests.test_main_matrix_reader import fixture as matrix_fixture, options as matrix_options
from tests.astronomy_workbench_fixtures import cube_bytes, tiff_bytes, selection as astronomy_options
from tests.alignment_browser_fixtures import encoded, OPTIONS as ALIGNMENT_OPTIONS
from tests.sequence_browser_fixtures import FASTA, FASTQ, BED, GFF, VCF, WIG, BEDGRAPH, BLAST12, BLAST13, sequence_options, blast_options

READERS = tuple(migration.KINDS)


def source(reader):
    if reader == "matrix-workbench":
        return matrix_fixture(np.arange(12).reshape(3,4)), "npy", "image", matrix_options([3,4])
    if reader == "astronomy-workbench":
        return cube_bytes(), "fits", "image", astronomy_options("pixel")
    if reader == "alignment-browser":
        with encoded("bam") as (data,_,__): return data, "bam", "table", dict(ALIGNMENT_OPTIONS)
    if reader == "sequence-browser": return FASTA, "fa", "table", sequence_options()
    if reader == "genome-tracks": return BED, "bed", "map", {"chromosome":0,"start":0,"end":200}
    return BLAST12, "m8", "table", blast_options()


def header(reader, data, fmt, kind="tree", options=None):
    return {"contract_version":2,"reader":reader,"size":len(data),"format":fmt,"kind":kind,"options":options or {},"truncated":False}


def wire(value, data=b"", extra=b""):
    return json.loads(worker._encoded_result(io.BytesIO(json.dumps(value).encode()+b"\n"+data+extra)))


def assert_selection(reader, value):
    if reader == "matrix-workbench": assert value["matrix"]["plane"]["values"] == np.arange(12).reshape(3,4).tolist()
    elif reader == "astronomy-workbench": assert value["workbench"]["value"] == 32
    elif reader == "alignment-browser":
        assert value["alignment"]["matched_reads"] == 5
        assert sum(v["covered_bases"] for v in value["alignment"]["coverage"]) == 38
    elif reader == "sequence-browser": assert value["sequence"]["bases"] == ("ACGT"*13)[:50]
    elif reader == "genome-tracks": assert [(v["start"],v["end"]) for v in value["tracks"]] == [(0,100),(80,180),(180,180)]
    else: assert value["hits"]["rows"][0]["evalue"] == "1e-350" and len(value["hits"]["rows"]) == 3


@pytest.mark.parametrize("reader",READERS)
def test_real_preview_dispatch_tree_and_selected_data(reader):
    data,fmt,kind,options=source(reader)
    tree=worker.preview_bytes(data,reader,"tree",{},format=fmt)
    assert tree["reader"] == tree["type"] == reader and tree["kind"] == "tree" and tree["contract_version"] == 2
    assert tree["metadata"]["source_bytes"] == len(data)
    selected=worker.preview_bytes(data,reader,kind,options,format=fmt)
    assert selected["selected"] == options and selected["metadata"]["source_bytes"] == len(data)
    assert_selection(reader,selected)


@pytest.mark.parametrize("reader",READERS)
@pytest.mark.parametrize("selection",[False,True])
def test_real_stdin_dispatch_exact_native_bytes(reader,selection):
    data,fmt,kind,options=source(reader)
    if not selection:kind,options="tree",{}
    result=wire(header(reader,data,fmt,kind,options),data)
    assert set(result)=={"ok","data"} and result["ok"] is True
    assert result["data"]["reader"]==reader and result["data"]["selected"]==options
    if selection:assert_selection(reader,result["data"])
    assert "/Users/private/never-return" not in json.dumps(result) and "https://private.invalid" not in json.dumps(result)


@pytest.mark.parametrize("reader",READERS)
@pytest.mark.parametrize("attack",["reader","format","kind","options","typed-options","truncated"])
def test_invalid_dispatch_has_fixed_errors_and_no_results(reader,attack):
    data,fmt,kind,options=source(reader);req=header(reader,data,fmt,kind,options)
    if attack=="reader":req["reader"]="/Users/private/unregistered"
    elif attack=="format":req["format"]="https://private.invalid/format"
    elif attack=="kind":req["kind"]="execute_python"
    elif attack=="options":req["options"]={**options,"path":"/Users/private/reference"}
    elif attack=="typed-options":req["options"]=["unexpected"]
    else:req["truncated"]=True
    with pytest.raises(worker.PreviewError):worker.preview_bytes(data,req["reader"],req["kind"],req["options"],format=req["format"],truncated=req["truncated"])
    result=wire(req,data);assert set(result)=={"ok","error"} and result["ok"] is False
    assert "private" not in result["error"] and "execute_python" not in result["error"]


@pytest.mark.parametrize("reader",READERS)
@pytest.mark.parametrize("attack",["short","extra","declared-short","declared-long","boolean-size","unknown-header","boolean-version","header-too-long"])
def test_wire_exact_length_and_header_fail_closed(reader,attack):
    data,fmt,_,__=source(reader);req=header(reader,data,fmt);extra=b""
    if attack=="short":data=data[:-1]
    elif attack=="extra":extra=b"must-not-be-another-frame"
    elif attack=="declared-short":req["size"]-=1
    elif attack=="declared-long":req["size"]+=1
    elif attack=="boolean-size":req["size"]=True
    elif attack=="unknown-header":req["source_path"]="/Users/private/file"
    elif attack=="boolean-version":req["contract_version"]=True
    else:req["options"]={"extra":"A"*8192}
    result=wire(req,data,extra);assert set(result)=={"ok","error"} and result["ok"] is False and "private" not in result["error"]


class SizedBytes(bytes):
    """Exercise entry byte-length gates without allocating oversized buffers."""
    def __new__(cls,size):
        value=super().__new__(cls,b"x");value.reported_size=size;return value
    def __len__(self):return self.reported_size


@pytest.mark.parametrize("reader",READERS)
def test_per_reader_outer_budget_rejected_before_parser(reader):
    _,fmt,_,__=source(reader)
    with pytest.raises(worker.PreviewError,match="预算"):
        worker.preview_bytes(SizedBytes(migration.input_limit(reader)+1),reader,"tree",{},format=fmt)


class HeaderOnlyStream(io.BytesIO):
    def __init__(self,req):super().__init__(json.dumps(req).encode()+b"\n");self.body_reads=[]
    def read(self,n=-1):self.body_reads.append(n);return b""


@pytest.mark.parametrize("reader",READERS)
def test_stdin_oversize_rejected_before_payload_read(reader):
    _,fmt,_,__=source(reader);req=header(reader,b"x",fmt);req["size"]=migration.input_limit(reader)+1
    stream=HeaderOnlyStream(req);assert json.loads(worker._encoded_result(stream))["ok"] is False
    assert stream.body_reads == []


def test_only_matrix_expands_source_budget_and_old_arrays_remain_unchanged():
    assert worker.MAX_INPUT_BYTES==64*1024**2 and worker.MAX_ARRAY_VALUES==16384 and worker.MAX_OUTPUT_BYTES==8*1024**2
    assert migration.input_limit("matrix-workbench")==128*1024**2
    for reader in worker.FORMATS.keys()-migration.KINDS.keys():
        assert migration.input_limit(reader)==64*1024**2
    for reader,fmt in [("tabular","npy"),("hdf5","h5"),("office","docx")]:
        kind=next(iter(worker.KINDS[reader]))
        with pytest.raises(worker.PreviewError,match="预算"):worker.preview_bytes(SizedBytes(64*1024**2+1),reader,kind,{},format=fmt)
        req=header(reader,b"x",fmt,kind);req["size"]=64*1024**2+1;stream=HeaderOnlyStream(req)
        assert json.loads(worker._encoded_result(stream))["ok"] is False and stream.body_reads==[]
    # The allowed matrix header can request >64 MiB but still needs exact bytes.
    req=header("matrix-workbench",b"x","npy");req["size"]=64*1024**2+1;stream=HeaderOnlyStream(req)
    assert json.loads(worker._encoded_result(stream))["ok"] is False and stream.body_reads==[64*1024**2+1]


def test_nested_reader_budgets_still_enforced_via_registered_entry():
    from astropy.io import fits
    # Compressed input is tiny: neither the outer byte gate nor format enum is
    # sufficient to keep an unbounded expansion or oversized result safe.
    bad_gzip=gzip.compress(b"\x00"*(32*1024**2+1))
    with pytest.raises(worker.PreviewError):worker.preview_bytes(bad_gzip,"astronomy-workbench","tree",{},format="fits.gz")
    stream=io.BytesIO();fits.PrimaryHDU(np.zeros((1536,1536),dtype=np.uint8)).writeto(stream)
    with pytest.raises(worker.PreviewError):worker.preview_bytes(stream.getvalue(),"astronomy-workbench","image",astronomy_options(slices=[]),format="fits")
    with pytest.raises(worker.PreviewError):worker.preview_bytes(FASTA,"sequence-browser","table",sequence_options(count=1001),format="fa")
    with pytest.raises(worker.PreviewError):worker.preview_bytes(BED*1001,"genome-tracks","map",{"chromosome":0,"start":0,"end":200},format="bed")
    with pytest.raises(worker.PreviewError):worker.preview_bytes(BLAST12,"blast-hits","table",blast_options(count=501),format="m8")
    data,fmt,_,opts=source("alignment-browser")
    with pytest.raises(worker.PreviewError):worker.preview_bytes(data,"alignment-browser","table",{**opts,"max_reads":2001},format=fmt)
    data,fmt,_,opts=source("matrix-workbench")
    with pytest.raises(worker.PreviewError):worker.preview_bytes(data,"matrix-workbench","image",{**opts,"max_points":513},format=fmt)
    # Options failure in a new reader cannot mutate a legacy global ceiling.
    assert worker.MAX_INPUT_BYTES==64*1024**2 and worker.MAX_ARRAY_VALUES==16384


@pytest.mark.parametrize("reader",READERS)
def test_framed_output_capture_budget_is_enforced(reader,monkeypatch):
    data,fmt,_,__=source(reader);monkeypatch.setattr(worker,"MAX_OUTPUT_BYTES",100)
    result=wire(header(reader,data,fmt),data)
    assert result["ok"] is False and set(result)=={"ok","error"}


def test_explicit_worker_formats_equal_each_domain_contract():
    from app.services.astronomy_workbench_payload import FORMATS as astronomy_formats
    from app.services.sequence_browser_payload import FORMATS as biological_formats
    assert worker.FORMATS["astronomy-workbench"]==astronomy_formats
    for reader,formats in biological_formats.items():assert worker.FORMATS[reader]==formats


@pytest.mark.parametrize("fmt",["fits","fit","fts","fz","fits.gz","tif","tiff"])
def test_astronomy_registered_aliases_not_generic_gzip(fmt):
    data=tiff_bytes() if fmt in {"tif","tiff"} else cube_bytes()
    if fmt=="fits.gz":data=gzip.compress(data)
    result=wire(header("astronomy-workbench",data,fmt),data);assert result["ok"] is True
    assert result["data"]["metadata"]["format"]==("tiff" if fmt in {"tif","tiff"} else "fits")
    with pytest.raises(worker.PreviewError):worker.preview_bytes(data,"astronomy-workbench","tree",{},format="gz")


@pytest.mark.parametrize("fmt",["sam","bam","cram"])
def test_alignment_native_formats_through_real_worker(fmt):
    with encoded(fmt,"no_ref") as (data,_,__):
        result=wire(header("alignment-browser",data,fmt,"table",dict(ALIGNMENT_OPTIONS)),data)
        assert result["ok"] is True;assert_selection("alignment-browser",result["data"])


@pytest.mark.parametrize("fmt",["fa","fasta","fna","ffn","frn","faa","fq","fastq"])
def test_sequence_aliases_and_explicit_quality_through_real_worker(fmt):
    quality=fmt in {"fq","fastq"};data=FASTQ if quality else FASTA;opts=sequence_options(quality_encoding="phred33" if quality else None)
    result=wire(header("sequence-browser",data,fmt,"table",opts),data);assert result["ok"] is True
    if quality:assert result["data"]["sequence"]["qualities"]==[0,20,30,40,41,42]


@pytest.mark.parametrize("fmt,data",[("bed",BED),("gff",GFF),("gff3",GFF),("vcf",VCF),("wig",WIG),("bedgraph",BEDGRAPH)])
def test_track_formats_through_real_worker(fmt,data):
    opts={"chromosome":0,"start":0,"end":200};result=wire(header("genome-tracks",data,fmt,"map",opts),data)
    assert result["ok"] is True and result["data"]["tracks"]


@pytest.mark.parametrize("fmt",["blast","blast6","blasttab","m8","tab"])
def test_blast_aliases_through_real_worker(fmt):
    result=wire(header("blast-hits",BLAST13,fmt,"table",blast_options()),BLAST13)
    assert result["ok"] is True and result["data"]["hits"]["rows"][0]["coverage"]==10
