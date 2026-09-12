"""Synthetic native SAM/BAM/CRAM fixtures; never uses user references/files."""
from contextlib import contextmanager
from pathlib import Path
import tempfile

import pysam


OPTIONS={"reference":0,"start":100,"end":160,"max_reads":20,"bins":20}
HEADER={"HD":{"VN":"1.6","SO":"coordinate"},"SQ":[{"SN":"chr1","LN":5000,"UR":"https://private.invalid/reference.fa"}],"RG":[{"ID":"rg1","SM":"hidden sample"}],"CO":["/Users/private/never-return"]}


def records(header=None):
    header=pysam.AlignmentHeader.from_dict(header or HEADER)
    specs=[("pair",99,100,"5M2I3M2D4M5N3M","AAG"+"A"*14,"2C5^AA7",4,140,62),
           ("duplicate",1024,104,"6M","A"*6,"6",0,-1,0),
           ("secondary",256,106,"4M","A"*4,"4",0,-1,0),
           ("without-md",0,125,"3M","AAA",None,None,-1,0),
           ("pair",147,140,"4=1X5=","AAAACAAAAA","4A5",1,100,-62)]
    output=[]
    for name,flag,start,cigar,seq,md,nm,mate,tlen in specs:
        r=pysam.AlignedSegment(header);r.query_name=name;r.flag=flag;r.reference_id=0;r.reference_start=start;r.mapping_quality=60;r.cigarstring=cigar;r.query_sequence=seq;r.query_qualities=pysam.qualitystring_to_array("I"*len(seq))
        r.next_reference_id=0 if mate>=0 else -1;r.next_reference_start=mate;r.template_length=tlen;r.set_tag("RG","rg1")
        if md is not None:r.set_tag("MD",md)
        if nm is not None:r.set_tag("NM",nm)
        output.append(r)
    return header,output


@contextmanager
def encoded(fmt="sam",cram_mode="no_ref",input_records=None,header=None):
    """Keep a valid external reference alive during a decoder denial test."""
    h,rs=records(header)
    if input_records is not None:rs=input_records
    with tempfile.TemporaryDirectory(prefix="alignment-native-fixture-") as directory:
        root=Path(directory);path=root/("fixture."+fmt);reference=root/"real-reference.fa"
        bases=list("A"*5000);bases[102]="C"
        reference.write_text(">chr1\n"+"".join(bases)+"\n")
        pysam.faidx(str(reference))
        settings={"header":h,"threads":1}
        if fmt=="cram":
            settings["reference_filename"]=str(reference)
            if cram_mode=="no_ref":settings["format_options"]=[b"no_ref=1"]
            elif cram_mode=="embedded":settings["format_options"]=[b"embed_ref=1"]
        with pysam.AlignmentFile(str(path),{"sam":"w","bam":"wb","cram":"wc"}[fmt],**settings) as output:
            for r in rs:output.write(r)
        yield path.read_bytes(),reference,path


def sam_bytes():
    with encoded() as (data,_,__):return data


def contract_fixture():
    from app.services.alignment_browser_reader import alignment_browser_preview
    data=sam_bytes()
    return {"tree":alignment_browser_preview(data,"sam"),"table":alignment_browser_preview(data,"sam","table",OPTIONS),
            "table_ui":alignment_browser_preview(data,"sam","table",{**OPTIONS,"bins":500})}
