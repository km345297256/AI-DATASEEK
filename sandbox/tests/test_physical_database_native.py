import os
from pathlib import Path
import pytest
from app.services.physical_database_reader import physical_database_preview, BIN
from test_physical_database_reader import fixture, payload


@pytest.fixture(autouse=True)
def binaries():
    if not all((BIN/name).is_file() for name in ("ibd2sdi","sst_dump")):
        if os.environ.get("REQUIRE_PHYSICAL_DATABASE_NATIVE") == "1": pytest.fail("Required offline utilities absent")
        pytest.skip("Offline binaries require candidate sandbox image")


@pytest.mark.parametrize("engine,name,fmt",[("mysql-sdi","mysql-8.0.46.ibd","ibd"),("sst-records","rocksdb-6.11.4.sst","sst")])
def test_real_offline_reader_matches_native_oracle(engine,name,fmt):
    assert physical_database_preview(fixture(name),engine,fmt)==payload(engine)


def test_native_checksum_and_range_fail_closed():
    with pytest.raises(ValueError): physical_database_preview(fixture("rocksdb-range.sst"),"sst-records","sst")
    original=fixture("rocksdb-6.11.4.sst")
    damaged=bytearray(original);damaged[12]^=1
    with pytest.raises(ValueError): physical_database_preview(bytes(damaged),"sst-records","sst")
    original=fixture("mysql-8.0.46.ibd")
    damaged=bytearray(original);damaged[3*16384+100]^=1
    with pytest.raises(ValueError): physical_database_preview(bytes(damaged),"mysql-sdi","ibd")


def test_native_leveldb_point_records():
    data=(Path(__file__).parent/"fixtures/physical-database/leveldb-1.23.ldb").read_bytes()
    from app.services.legacy_sst_guard import legacy_rows
    result=physical_database_preview(data,"sst-records","ldb")
    assert result["table"]["rows"]==legacy_rows(data)
    assert [r[2] for r in result["table"]["rows"]]==["1","2","3"]
