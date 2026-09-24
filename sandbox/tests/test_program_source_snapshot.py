"""A bounded method snapshot must be the very bytes supplied to execution."""
import hashlib,json
from types import SimpleNamespace
import pytest
from app.services.program import prepare_program,program_feedback,MAX_REVIEW_SOURCE_BYTES

def prepared(tmp_path,raw):
 p=tmp_path/'method.py';p.write_bytes(raw);return p,prepare_program(str(p),[])

def test_source_snapshot_exact_and_immutable_after_original_replacement(tmp_path):
 original=b'print("original")\r\n';p,(fd,diag,metadata)=prepared(tmp_path,original)
 try:
  p.write_bytes(b'print("replacement")\n')
  assert fd.read()==original
  snap=metadata['source_snapshot'];assert snap['content'].encode('utf-8')==original
  assert snap['sha256']==metadata['source_digest']==hashlib.sha256(original).hexdigest()
  assert snap['size_bytes']==len(original)
 finally:fd.close();diag.close()

@pytest.mark.parametrize('raw',[b'# coding: latin-1\nvalue="\xe9"',b'x'*(MAX_REVIEW_SOURCE_BYTES+1),'汉'.encode()*8001])
def test_non_utf8_or_oversize_source_remains_executable_snapshot_but_not_false_full(tmp_path,raw):
 _,(fd,diag,metadata)=prepared(tmp_path,raw)
 try:
  assert fd.read()==raw
  assert 'source_snapshot' not in metadata
 finally:fd.close();diag.close()

@pytest.mark.parametrize('raw',[b'',b'x'*MAX_REVIEW_SOURCE_BYTES,'汉'.encode()*8000,b'\xef\xbb\xbfprint(1)\n'])
def test_exact_boundary_empty_unicode_and_bom_are_byte_faithful(tmp_path,raw):
 _,(fd,diag,metadata)=prepared(tmp_path,raw)
 try:assert metadata['source_snapshot']['content'].encode()==raw
 finally:fd.close();diag.close()

@pytest.mark.parametrize('returncode',[None,0,1])
def test_feedback_keeps_snapshot_tied_to_launch_while_backend_decides_success(tmp_path,returncode):
 _,(fd,diag,metadata)=prepared(tmp_path,b'print(1)\n');fd.close()
 shell={'program_execution':metadata,'process':SimpleNamespace(returncode=returncode),'program_diagnostics':diag}
 try:
  feedback=program_feedback(shell)
  assert feedback['source_snapshot']['sha256']==metadata['source_digest']
  assert feedback['returncode']==returncode
 finally:
  if not diag.closed:diag.close()
