"""Generate ORIGINAL physical-file fixtures in a disposable test container only.

Requires Ubuntu's fixed MySQL 8.0.46 and RocksDB 6.11.4 packages. Never pass a
user database directory. The sole persistent output is the supplied fixture dir.
"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def run(args, **kwargs):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=120, **kwargs).stdout


def main():
    target = Path(sys.argv[1])
    target.mkdir(parents=True, exist_ok=True)
    assert not list(target.iterdir()), "Fixture output must be empty"
    with tempfile.TemporaryDirectory(prefix="original-physical-fixture-") as tmp:
        root = Path(tmp)
        data = root / "mysql"
        run(["mysqld", "--no-defaults", "--initialize-insecure", "--user=root", f"--datadir={data}"])
        socket = root / "mysql.sock"
        server = subprocess.Popen(["mysqld", "--no-defaults", "--user=root", f"--datadir={data}",
            "--skip-networking", "--mysqlx=OFF", "--secure-file-priv=NULL", f"--socket={socket}", f"--pid-file={root / 'mysql.pid'}", "--innodb-buffer-pool-size=64M"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            for _ in range(100):
                if socket.exists(): break
                assert server.poll() is None, server.stderr.read().decode()
                time.sleep(.1)
            sql = b"CREATE DATABASE fixture; CREATE TABLE fixture.measurements (id BIGINT PRIMARY KEY, label VARCHAR(40) NOT NULL, reading DECIMAL(20,4), INDEX by_label(label)); INSERT INTO fixture.measurements VALUES (1,'original',1.2500);"
            run(["mysql", "--no-defaults", "-uroot", f"--socket={socket}"], input=sql)
        finally:
            server.terminate()
            server.wait(timeout=30)
        shutil.copyfile(data / "fixture/measurements.ibd", target / "mysql-8.0.46.ibd")
        raw = run(["ibd2sdi", "--skip-pretty", "--strict-check=crc32", str(target / "mysql-8.0.46.ibd")])
        (target / "mysql-sdi-oracle.json").write_bytes(raw)
        source = root / "writer.cc"
        source.write_text('''#include <rocksdb/sst_file_writer.h>
#include <rocksdb/options.h>
#include <cstdlib>
void ok(rocksdb::Status s) { if(!s.ok()) std::abort(); }
int main(int argc,char**argv) { rocksdb::Options o; o.compression=rocksdb::kNoCompression;
 rocksdb::SstFileWriter w(rocksdb::EnvOptions(),o); ok(w.Open(argv[1]));
 ok(w.Put("a", "9007199254740993")); ok(w.Delete("b")); ok(w.Put("c", std::string("a\\0b",3)));
 if(argc>2) ok(w.DeleteRange("d","z")); ok(w.Finish()); }
''')
        run(["g++", "-std=c++14", str(source), "-lrocksdb", "-o", str(root / "writer")])
        for name, extra in [("rocksdb-6.11.4.sst", []), ("rocksdb-range.sst", ["range"])]:
            run([str(root / "writer"), str(target / name), *extra])
            output = run(["sst_dump", "--file=" + str(target / name), "--command=scan", "--output_hex", "--verify_checksum", "--show_properties"])
            # Only these ORIGINAL fixture paths appear in oracle output.
            (target / (name + ".oracle.txt")).write_bytes(output.replace(str(target).encode(), b"FIXTURE"))
    manifest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(target.iterdir())}
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__": main()
