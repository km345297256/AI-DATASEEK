"""Print original synthetic trees from the real reader; no input files or writes."""
import json
from app.services.phylogeny_reader import phylogeny_preview


def payloads():
    return {key: phylogeny_preview(source.encode(), "nwk") for key, source in {
        "complete": "((Human:0.1,Chimp:0.2)95:0.3,Mouse:0.8)Root:0.01;",
        "missing": "((Human,Chimp:0.2)95:0.3,Mouse:0.8)Root;",
        "zero": "(Human:0,Chimp:0)Root:5;",
    }.items()}


if __name__ == "__main__":
    print(json.dumps(payloads(), ensure_ascii=False, indent=2))
