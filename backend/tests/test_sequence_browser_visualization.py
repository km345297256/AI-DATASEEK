"""Host-side contract firewall tests, without scientific packages or file IO."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
from app.application.services.sequence_browser_visualization import validate_payload, validate_options

DATA = json.loads((Path(__file__).parent / 'fixtures/main_bio_data.json').read_text())


@pytest.mark.parametrize('name', DATA)
def test_real_reader_results_pass_host_schema(name):
    value = DATA[name]
    assert validate_payload(value, reader=value['reader'], kind=value['kind'], options=value['selected'], format=value['metadata']['format'], source_bytes=value['metadata']['source_bytes']) == value


@pytest.mark.parametrize('name', ['fasta','fastq','bed','gff','vcf','wig','bedgraph','blast12','blast13'])
@pytest.mark.parametrize('wrong', ['reader','kind','options','format','size','extra','metadata','sampled','warning','version','limit'])
def test_worker_cannot_forge_domain_results(name, wrong):
    value = deepcopy(DATA[name]);kwargs=dict(reader=value['reader'],kind=value['kind'],options=value['selected'].copy(),format=value['metadata']['format'],source_bytes=value['metadata']['source_bytes'])
    if wrong=='reader':kwargs['reader']='other'
    if wrong=='kind':kwargs['kind']='tree'
    if wrong=='options':kwargs['options']={}
    if wrong=='format':kwargs['format']='exe'
    if wrong=='size':kwargs['source_bytes']+=1
    if wrong=='extra':value['url']='https://invalid'
    if wrong=='metadata':value['metadata']['path']='/Users/private'
    if wrong=='sampled':value['sampled']=True
    if wrong=='warning':value['warnings']=[]
    if wrong=='version':value['contract_version']=True
    if wrong=='limit':kwargs['limit']=1
    with pytest.raises(ValueError):validate_payload(value,**kwargs)


def test_coordinates_and_blast_semantics_are_checked_again():
    for key,edit in [
        ('bed',lambda v:v['tracks'][0].update(start=-1)),
        ('bed',lambda v:v['tracks'][0].update(end=2147483648)),
        ('bed',lambda v:v['tracks'][0].update(chromosome=1)),
        ('bed',lambda v:v['tracks'][0].update(track='variant')),
        ('fasta',lambda v:v['sequence'].update(bases='http://invalid')),
        ('fastq',lambda v:v['sequence']['qualities'].append(1)),
        ('blast12',lambda v:v['hits']['rows'][0].update(coverage=100)),
        ('blast13',lambda v:v['hits']['rows'][0].update(coverage=100)),
        ('blast13',lambda v:v['hits']['rows'][0].update(evalue='NaN')),
    ]:
        value=deepcopy(DATA[key]);edit(value)
        with pytest.raises(ValueError):validate_payload(value,reader=value['reader'])


@pytest.mark.parametrize('reader', ['sequence-browser','genome-tracks','blast-hits'])
def test_unknown_options_rejected_before_io(reader):
    with pytest.raises(ValueError):validate_options(reader,'tree',{'url':'https://invalid'})
