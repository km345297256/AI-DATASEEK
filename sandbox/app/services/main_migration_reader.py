"""Trusted dispatch for main's workbenches, inside the one-shot worker only."""
FORMATS = {
    "matrix-workbench": {"npy", "npz", "mtx", "mat"},
    "astronomy-workbench": {"fits", "fits.gz", "fit", "fts", "fz", "tif", "tiff"},
    "alignment-browser": {"sam", "bam", "cram"},
    "sequence-browser": {"fa", "fasta", "fna", "faa", "ffn", "frn", "fq", "fastq"},
    "genome-tracks": {"vcf", "gff", "gff3", "gtf", "bed", "bedgraph", "wig"},
    "blast-hits": {"blast", "blast6", "blasttab", "m8", "tab"},
}
KINDS = {"matrix-workbench":{"tree", "image", "series"}, "astronomy-workbench":{"tree", "image", "series", "table"},
         "alignment-browser":{"tree", "table"}, "sequence-browser":{"tree", "table"},
         "genome-tracks":{"tree", "map"}, "blast-hits":{"tree", "table"}}


def input_limit(reader):
    return {"matrix-workbench":128, "astronomy-workbench":32, "alignment-browser":64,
            "sequence-browser":16, "genome-tracks":16, "blast-hits":16}.get(reader, 64) * 1024**2


def preview(data, reader, kind, options, fmt):
    if reader == "matrix-workbench":
        from .main_matrix_reader import main_matrix_preview
        return main_matrix_preview(data, fmt, kind, options)
    if reader == "astronomy-workbench":
        from .astronomy_workbench_reader import astronomy_workbench_preview
        return astronomy_workbench_preview(data, fmt, kind, options)
    if reader == "alignment-browser":
        from .alignment_browser_reader import alignment_browser_preview
        return alignment_browser_preview(data, fmt, kind, options)
    if reader in {"sequence-browser", "genome-tracks", "blast-hits"}:
        from .sequence_browser_reader import sequence_browser_preview
        return sequence_browser_preview(data, reader, fmt, kind, options)
    raise ValueError("Unsupported migration reader")
