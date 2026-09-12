from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import pytest
from app.services.sequence_browser_reader import BLAST_COLUMNS, sequence_browser_preview as preview
from app.services.sequence_browser_payload import validate_options, validate_payload

from tests.sequence_browser_fixtures import (FASTA, FASTQ, BED, GFF, VCF, WIG, BEDGRAPH, BLAST12, BLAST13,
    sequence_options, blast_options, fixtures)

@pytest.mark.parametrize('key', list(fixtures()))
def test_valid_reader_and_pure_boundary(key):
    value=fixtures()[key]
    assert validate_payload(value,reader=value['reader'],kind=value['kind'],options=value['selected'],format=value['metadata']['format'],source_bytes=value['metadata']['source_bytes']) is value


def test_multiline_phred33_and_case_statistics():
    value=fixtures()['fastq'];s=value['sequence']
    assert s['bases']=='ACGTNN' and s['qualities']==[0,20,30,40,41,42]
    assert s['quality_stats']==dict(sum=173,low_count=1)
    assert fixtures()['fasta']['choices']['records'][1]['length']==9
    assert fixtures()['fasta']['choices']['records'][0]['gc_count']==600


def test_explicit_phred64_never_autodetected():
    data=b'@r\nACT\n+\n@AB\n'
    assert preview(data,'sequence-browser','fq','table',sequence_options(quality_encoding='phred64'))['sequence']['qualities']==[0,1,2]
    assert preview(data,'sequence-browser','fq','table',sequence_options(quality_encoding='phred33'))['sequence']['qualities']==[31,32,33]
    with pytest.raises(ValueError): preview(FASTQ,'sequence-browser','fq','table',sequence_options(quality_encoding='phred64'))
    with pytest.raises(ValueError): preview(FASTQ,'sequence-browser','fq','table',sequence_options())


def test_motif_matches_overlap_and_exact_window():
    value=preview(b'>r\nAAAAA\n','sequence-browser','fa','table',sequence_options(start=2,count=2,motif='AAA'))
    assert value['sequence']['search']==dict(positions=[1,2,3],total=3,truncated=False)
    assert value['sequence']['bases']=='AA'


def test_search_truncation_is_explicit_but_total_exact():
    value=preview(b'>r\n'+b'A'*10010,'sequence-browser','fa','table',sequence_options(motif='A'))
    assert len(value['sequence']['search']['positions'])==10000
    assert value['sequence']['search']['total']==10010 and value['sequence']['search']['truncated']


@pytest.mark.parametrize('data', [b'>r\n',b'ACGT',b'>r\nAC1T',b'>r\nAC\x00',b'@r\nACGT\n+\nIII',b'@r\nAC\n+x\nII',b'@r\nAC\n+\nI I',b'@r\nAC\n+\nIII',b'@r\nAC\n+\nII\nnoise'])
def test_bad_sequence_rejected_not_silently_skipped(data):
    with pytest.raises(ValueError): preview(data,'sequence-browser','fq' if data.startswith(b'@') else 'fa')


def test_input_limits_record_limits_and_utf8():
    with pytest.raises(ValueError): preview(b'>r\n'+b'A'*8000001,'sequence-browser','fa')
    with pytest.raises(ValueError): preview(b'>r\nA\n'*257,'sequence-browser','fa')
    with pytest.raises(ValueError): preview(b'>r\n\xff','sequence-browser','fa')
    with pytest.raises(ValueError): preview(b' '*16777217,'sequence-browser','fa')


def test_coordinates_and_strands_preserved():
    f=fixtures()
    assert [(x['start'],x['end'],x['strand']) for x in f['bed']['tracks']]==[(0,100,'+'),(80,180,'-'),(180,180,'.')]
    assert [(x['start'],x['end']) for x in f['gff']['tracks']]==[(0,20)]
    assert [(x['start'],x['end']) for x in f['vcf']['tracks']]==[(9,11),(49,100)]
    assert [(x['start'],x['end'],x['value']) for x in f['wig']['tracks']]==[(0,2,4),(10,12,-2)]
    assert [(x['start'],x['end'],x['value']) for x in f['bedgraph']['tracks']]==[(0,10,4),(20,30,-2)]
    assert preview(WIG,'genome-tracks','wig','map',dict(chromosome=1,start=0,end=30))['tracks'][0]['start']==10


def test_interval_boundary_and_empty_viewport_not_fake_points():
    opts=dict(chromosome=0,start=180,end=181)
    assert [x['label'] for x in preview(BED,'genome-tracks','bed','map',opts)['tracks']]==['insertion']
    assert not preview(BED,'genome-tracks','bed','map',dict(chromosome=0,start=200,end=300))['tracks']


def test_vcf_idless_alleles_keep_scientific_labels_without_markup():
    raw=b'chr1\t10\t.\tA\tG\t20\tPASS\t.\nchr1\t50\t.\tN\t<DEL>\t.\tPASS\tEND=100\n'
    v=preview(raw,'genome-tracks','vcf','map',dict(chromosome=0,start=0,end=100))
    assert [f['label'] for f in v['tracks']]==['A→G','N→〈DEL〉']
    assert '〈DEL〉' in v['tracks'][1]['detail']
    assert v['metadata']['labels_redacted']==0
    assert [(f['start'],f['end']) for f in v['tracks']]==[(9,10),(49,100)]


@pytest.mark.parametrize('fmt,data',[('bed',b'chr1 10 9'),('bed',b'chr1 0 1 a\nchr1 1 2'),('bedgraph',b'chr1 0 1 NaN'),('bedgraph',b'chr1 0 0 2'),('gff3',b'chr1\ts\tgene\t0\t10\t.\t+\t.\tx'),('vcf',b'chr1\t1\t.\tN\tN[chr2:4[\t.\tPASS\t.'),('wig',b'1 2'),('wig',b'variableStep chrom=chr1 span=1\n2 1\n1 2'),('wig',b'fixedStep chrom=chr1 start=1 step=1 url=http://bad\n2')])
def test_malformed_tracks_reject(fmt,data):
    with pytest.raises(ValueError): preview(data,'genome-tracks',fmt)


def test_tracks_budgets_reject_not_truncate():
    data=b'chr1\t0\t10\n'*3001
    assert preview(data,'genome-tracks','bed')['metadata']['records']==3001
    with pytest.raises(ValueError): preview(data,'genome-tracks','bed','map',dict(chromosome=0,start=0,end=20))
    with pytest.raises(ValueError): preview(b''.join(f'chr{i}\t0\t1\n'.encode() for i in range(257)),'genome-tracks','bed')


def test_labels_are_inert_no_host_paths_or_urls():
    value=preview(b'chr1\t0\t10\t/Users/private/project\t5\t+\n','genome-tracks','bed','map',dict(chromosome=0,start=0,end=20))
    assert '/Users/' not in json.dumps(value) and value['metadata']['labels_redacted']==2
    value=preview(b'>https://example.org/private\nACTG\n','sequence-browser','fa')
    assert 'https:' not in json.dumps(value) and value['choices']['records'][0]['name']=='Sequence 1'


def test_blast_unknown_coverage_small_evalue_and_direction():
    b=fixtures()['blast12'];hits=b['hits']['rows']
    assert all(h['coverage'] is None for h in hits) and hits[0]['evalue']=='1e-350'
    assert hits[1]['qstart']==400 and hits[1]['qend']==301
    filtered=preview(BLAST12,'blast-hits','m8','table',blast_options(min_coverage=1))
    assert filtered['hits']==dict(rows=[],total=0,unknown_coverage=3)


def test_blast_declared_qlen_correct_not_capped_span():
    b=fixtures()['blast13']
    assert [h['coverage'] for h in b['hits']['rows']]==[10,10]
    assert len(fixtures()['blast_filter']['hits']['rows'])==1
    assert preview(BLAST12,'blast-hits','m8','table',blast_options(offset=2,count=1))['hits']['rows'][0]['query']==1


@pytest.mark.parametrize('data',[BLAST13.split(b'\n',1)[1],BLAST12.replace(b'99\t100',b'101\t100'),BLAST12.replace(b'1e-350',b'-1'),BLAST12.replace(b'1e-350',b'NaN'),BLAST12.replace(b'1\t100\t20',b'0\t100\t20'),b'# Fields: query id, subject id, evalue\n'+BLAST12,BLAST13.replace(b'1000',b'10'),b'garbage'])
def test_blast_bad_and_ambiguous_schema_rejected(data):
    with pytest.raises(ValueError):preview(data,'blast-hits','m8')


@pytest.mark.parametrize('reader,kind,options',[('sequence-browser','table',sequence_options(record=True)),('sequence-browser','table',sequence_options(quality_encoding={})),('sequence-browser','table',sequence_options(count=1001)),('sequence-browser','table',sequence_options(motif='.*[AC]')),('genome-tracks','map',dict(chromosome=True,start=0,end=1)),('genome-tracks','map',dict(chromosome=0,start=1,end=1)),('blast-hits','table',blast_options(min_identity=True)),('blast-hits','table',blast_options(offset=-1)),('blast-hits','tree',dict(path='/tmp/a'))])
def test_options_strict_before_read(reader,kind,options):
    with pytest.raises(ValueError):validate_options(reader,kind,options)


@pytest.mark.parametrize('key',['fasta','fastq','bed','blast12','blast13'])
@pytest.mark.parametrize('mutation',['extra','selected','kind','source','format','metadata','warning','sampled','limits'])
def test_boundary_rejects_mutated_worker_payload(key,mutation):
    value=deepcopy(fixtures()[key]);reader=value['reader'];kind=value['kind'];options=value['selected'].copy();size=value['metadata']['source_bytes'];fmt=value['metadata']['format']
    if mutation=='extra':value['url']='https://invalid'
    if mutation=='selected':value['selected']={}
    if mutation=='kind':value['kind']='tree'
    if mutation=='source':value['metadata']['source_bytes']+=1
    if mutation=='format':value['metadata']['format']='exe'
    if mutation=='metadata':value['metadata']['path']='/Users/secret'
    if mutation=='warning':value['warnings']=[]
    if mutation=='sampled':value['sampled']=True
    if mutation=='limits':value['metadata']['limits']={}
    with pytest.raises(ValueError):validate_payload(value,reader=reader,kind=kind,options=options,format=fmt,source_bytes=size)


def test_payload_tampering_scientific_invariants():
    changes=[('fasta',lambda v:v['sequence'].update(bases='BAD')),('fastq',lambda v:v['sequence']['qualities'].append(4)),('bed',lambda v:v['tracks'][0].update(start=-1)),('bed',lambda v:v['tracks'][0].update(track='variant')),('blast13',lambda v:v['hits']['rows'][0].update(coverage=100)),('blast12',lambda v:v['hits']['rows'][0].update(coverage=100))]
    for key,change in changes:
        value=deepcopy(fixtures()[key]);change(value)
        with pytest.raises(ValueError):validate_payload(value,reader=value['reader'])


def test_payload_output_budget_and_source_binding():
    value=fixtures()['fasta']
    with pytest.raises(ValueError):validate_payload(value,reader='sequence-browser',limit=1)
    with pytest.raises(ValueError):validate_payload(value,reader='sequence-browser',source_bytes=True)
