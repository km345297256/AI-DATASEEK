"""Small original synthetic biological files; no pytest or external data."""
from app.services.sequence_browser_reader import BLAST_COLUMNS, sequence_browser_preview as preview
FASTA = b'>alpha sequence\n' + b'ACGT' * 300 + b'NN\n>beta\nMPEPTIDE*\n'
FASTQ = b'@read one\nACGT\nNN\n+read\n!5?I\nJK\n'
BED = b'chr1\t0\t100\tforward\t40\t+\nchr1\t80\t180\treverse\t80\t-\nchr1\t180\t180\tinsertion\t0\t.\nchr2\t10\t20\tother\t2\t+\n'
GFF = b'##gff-version 3\nchr1\tsource\tgene\t1\t20\t.\t-\t.\tID=g1;Name=Gene1\n'
VCF = b'##fileformat=VCFv4.3\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\nchr1\t10\tdel\tAT\tA\t20\tPASS\t.\nchr1\t50\tsv\tN\t<DEL>\t.\tPASS\tEND=100\n'
WIG = b'track type=wiggle_0\nfixedStep chrom=chr1 start=1 step=10 span=2\n4\n-2\nvariableStep chrom=chr2 span=3\n11 6\n'
BEDGRAPH = b'chr1\t0\t10\t4\nchr1\t20\t30\t-2\n'
BLAST12 = b'q1\tsubject-forward\t99\t100\t1\t0\t1\t100\t20\t119\t1e-350\t150\nq1\tsubject-reverse\t80\t100\t20\t0\t400\t301\t900\t801\t3e-12\t80\nq2\tsubject-single\t100\t1\t0\t0\t1\t1\t10\t10\t0\t12\n'
BLAST13 = ('# Fields: ' + ', '.join(BLAST_COLUMNS + ['query length']) + '\n').encode() + b'q\tsubject-forward\t99\t100\t1\t0\t1\t100\t20\t119\t1e-350\t150\t1000\nq\tsubject-reverse\t80\t100\t20\t0\t400\t301\t900\t801\t3e-12\t80\t1000\n'


def sequence_options(**changes):
    return dict(record=0, start=1, count=50, motif='', quality_encoding=None) | changes


def blast_options(**changes):
    return dict(query=None, min_identity=0, min_coverage=0, offset=0, count=100) | changes


def fixtures():
    out = {}
    for key, reader, fmt, data in [('fasta','sequence-browser','fa',FASTA),('fastq','sequence-browser','fq',FASTQ),('bed','genome-tracks','bed',BED),('gff','genome-tracks','gff3',GFF),('vcf','genome-tracks','vcf',VCF),('wig','genome-tracks','wig',WIG),('bedgraph','genome-tracks','bedgraph',BEDGRAPH),('blast12','blast-hits','m8',BLAST12),('blast13','blast-hits','blast',BLAST13)]:
        out[key+'_tree'] = preview(data,reader,fmt)
    for key, data, fmt, options in [('fasta',FASTA,'fa',sequence_options()),('fasta_search',FASTA,'fa',sequence_options(motif='ACG')),('fasta_next',FASTA,'fa',sequence_options(start=5,motif='ACG')),('fastq',FASTQ,'fq',sequence_options(quality_encoding='phred33'))]:
        out[key] = preview(data,'sequence-browser',fmt,'table',options)
    for key,data,fmt,end in [('bed',BED,'bed',200),('gff',GFF,'gff3',100),('vcf',VCF,'vcf',100),('wig',WIG,'wig',100),('bedgraph',BEDGRAPH,'bedgraph',100)]:
        out[key] = preview(data,'genome-tracks',fmt,'map',dict(chromosome=0,start=0,end=end))
    for key,data,fmt,options in [('blast12',BLAST12,'m8',blast_options()),('blast13',BLAST13,'blast',blast_options()),('blast_filter',BLAST13,'blast',blast_options(min_identity=90,min_coverage=5))]:
        out[key] = preview(data,'blast-hits',fmt,'table',options)
    return out

