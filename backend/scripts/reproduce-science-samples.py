"""Reproduce only the reviewed NOAA/Newick format normalizations, offline.

Default verifies existing outputs without writes. --write creates missing
derived files exclusively; originals and existing outputs are never replaced.
The root contains viz-* subdirectories from the reviewed sample catalog.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
from zipfile import ZipFile


def checked(path, expected_sha, expected_size):
    if path.resolve() != path.absolute() or path.stat().st_size != expected_size:
        raise ValueError("Unexpected source path or size")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha:
        raise ValueError("Source differs from reviewed snapshot")
    return data


def output(path, data, expected_sha, write):
    if hashlib.sha256(data).hexdigest() != expected_sha:
        raise ValueError("Reproduced bytes differ from reviewed output")
    if path.resolve() != path.absolute():
        raise ValueError("Symlinked destination")
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError("Existing output differs; refusing overwrite")
    elif write:
        with path.open("xb") as stream:
            stream.write(data)
    else:
        raise ValueError("Missing output; use --write in staging")
    return {"file": path.name, "bytes": len(data), "sha256": expected_sha}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    root = args.root.absolute()
    if root.resolve() != root or not root.is_dir():
        raise ValueError("Root must be an existing non-symlink directory")
    methane = root / "viz-noaa-global-methane"
    original = checked(methane / "ch4_mm_gl_original.csv", "c45ca56872999f8d8e8e04f9f3523c2599d6358960171dd0a0b374eb89be3163", 23937)
    clean = b"".join(line for line in original.splitlines(keepends=True) if line.strip() and not line.lstrip().startswith(b"#"))
    results = [output(methane / "ch4_mm_gl.csv", clean, "dd6b1477f17dec4a46c7d7e2fde12c07a3969c1ac16a2c044c1a6b082be27ea2", args.write)]
    tree = root / "viz-elife-seph-phylogeny"
    archive_path = tree / "elife-63387-fig7-data1-v1.zip"
    checked(archive_path, "261ae59ae31abf8e5164b98535f1ea66d6906a13787f66aa6d1b6273b29cd732", 47094)
    with ZipFile(archive_path) as archive:
        member = archive.getinfo("Figure 7-Source_data_1/compare80phy_phyml_tree.txt")
        if member.file_size != 19112 or member.flag_bits & 1:
            raise ValueError("Unexpected or encrypted tree member")
        original_tree = archive.read(member)
    results.append(output(tree / "seph_phyml.nwk", original_tree, "208f8aaf13755273416ab5313346a37182f7b5337ed6e12b2d5d8b6722dafb92", args.write))
    # Exactly one reviewed literal label; never rewrite tree topology or lengths.
    text = original_tree.decode("utf-8")
    matches = re.findall(r"(?<=[(,])\[Propionibacterium\]_humerusii_(?=:)", text)
    if len(matches) != 1:
        raise ValueError("Expected exactly one known taxon label")
    quoted = re.sub(r"(?<=[(,])\[Propionibacterium\]_humerusii_(?=:)", "'[Propionibacterium]_humerusii_'", text).encode("utf-8")
    results.append(output(tree / "seph_phyml_quoted.nwk", quoted, "67700ba0fd792e5190732a1d3b670b433bdca0e4de0d5ceb64a8d486d1ae4d60", args.write))
    print(json.dumps({"mode": "write-missing-only" if args.write else "read-only", "results": results}))


if __name__ == "__main__":
    main()
