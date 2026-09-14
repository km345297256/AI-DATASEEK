from pathlib import Path
import pytest
from app.services.legacy_sst_guard import legacy_rows, crc32c, varint

DATA=(Path(__file__).parent/"fixtures/physical-database/leveldb-1.23.ldb").read_bytes()


def test_crc32c_known_vector_and_native_writer():
    assert crc32c(b"123456789")==0xe3069283
    rows=legacy_rows(DATA)
    assert rows[0]==["61","1","1","value","6f726967696e616c","8","none"]
    assert rows[1]==["62","1","2","deletion","","0","none"]
    assert rows[2][-3:]==["610062","3","none"]


@pytest.mark.parametrize("position", range(len(DATA)))
def test_every_byte_corruption_rejected(position):
    data=bytearray(DATA);data[position]^=1
    with pytest.raises(ValueError): legacy_rows(bytes(data))


@pytest.mark.parametrize("size",range(len(DATA)))
def test_every_truncation_rejected(size):
    with pytest.raises(ValueError): legacy_rows(DATA[:size])


@pytest.mark.parametrize("value",[b"\x80",b"\x80\x00",b"\xff"*10,b"\x81"*10+b"\x00"])
def test_overlong_varints_rejected(value):
    with pytest.raises(ValueError): varint(value,0)
