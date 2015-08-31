#! /usr/bin/env python

import argparse
import sys
import os
import pysam
from collections import defaultdict
import pandas as pd
import numpy as np
import multiprocessing as mp
from yeti.genomics.genome_array import HashedReadBAMGenomeArray, ReadKeyMapFactory, read_length_nmis
from yeti.genomics.roitools import Transcript

parser = argparse.ArgumentParser()
parser.add_argument('orfstore', help='Path to pandas HDF store containing ORFs to regress; generated by find_ORFs.py')
parser.add_argument('cdsstore', help='Path to pandas HDF store containing CDS information; generated by find_annotated_CDSs.py')
parser.add_argument('tfamstem', help='Transcript family information generated by make_tfams.py. '
                                     'Both TFAMSTEM.txt and TFAMSTEM.bed should exist or an error will result.')
parser.add_argument('offset', help='Path to 2-column tab-delimited file with 5\' offsets for variable P-site mappings. First column indicates read '
                                   'length, second column indicates offset to apply. Read lengths are calculated after trimming 5\' mismatches. '
                                   'Accepted read lengths are defined by those present in the first column of this file.')
parser.add_argument('bamfiles', nargs='+', help='Path to transcriptome-aligned BAM file(s) for read data')
parser.add_argument('outfile', help='Filename to which to output the table of regression scores for each ORF. Formatted as pandas HDF (preferred '
                                    'extension is .h5; tables generated include "start_strengths", "ORF_strengths", and "stop_strengths")')
parser.add_argument('--inbed', type=argparse.FileType('rU'), default=sys.stdin, help='Transcriptome BED-file (Default: stdin)')
parser.add_argument('--startonly', action='store_true', help='Toggle for datasets collected in the presence of initiation inhibitor (e.g. Harr, '
                                                             'LTM). If selected, only "start_strengths" will be calculated and saved.')
parser.add_argument('--startcodons', type=int, nargs=2, default=[1, 50],
                    help='Region around start codon (in codons) to model explicitly. Ignored if reading metagene from file (Default: 1 30, meaning '
                         'one full codon before the start is modeled, as are the start codon and the 49 codons following it).')
parser.add_argument('--stopcodons', type=int, nargs=2, default=[7, 0],
                    help='Region around stop codon (in codons) to model explicitly. Ignored if reading metagene from file (Default: 7 0, meaning '
                         'seven full codons before and including the stop are modeled, but none after).')
parser.add_argument('--mincdsreads', type=int, default=64,
                    help='Minimum number of reads required within the body of the CDS (and any surrounding nucleotides indicated by STARTCODONS or '
                         'STOPNT) for it to be included in the metagene. Ignored if reading metagene from file (Default: 64).')
parser.add_argument('--startcount', type=int, default=0,
                    help='Minimum reads at putative translation initiation codon. Useful to reduce computational burden by only considering ORFs '
                         'with e.g. at least 1 read at the start. (Default: 0)')
# parser.add_argument('--minrdlen', type=int, default=27, help='Minimum permitted read length')
# parser.add_argument('--maxrdlen', type=int, default=34, help='Maximum permitted read length (inclusive)')
# min and max read length inferred from the offset file
parser.add_argument('--max5mis', type=int, default=1, help='Maximum 5\' mismatches to trim. Reads with more than this number will be excluded.'
                                                           '(Default: 1)')
parser.add_argument('--metagenefile', help='File to save metagene profile, OR if the file already exists, it will be used as the input metagene. '
                                           'File is formatted as a pickle of three numpy arrays (startprof, cdsprof, and stopprof).')
parser.add_argument('-p', '--numproc', type=int, default=1, help='Number of processes to run. Defaults to 1 but recommended to use more (e.g. 12-16)')
opts = parser.parse_args()

inbams = [pysam.Samfile(infile) for infile in opts.bamfiles]  # defaults to read mode, and will figure out if it's BAM or SAM - though we require BAM
rdlens = []
Pdict = {}
with open(opts.offset, 'rU') as infile:
    for line in infile:
        ls = line.strip().split()
        rdlen = int(ls[0])
        for nmis in range(opts.max5mis+1):
            Pdict[(rdlen, nmis)] = int(ls[1])+nmis  # e.g. if nmis == 1, offset as though the read were missing that base entirely
        rdlens.append(rdlen)
    # Pdict = {(int(ls[0]), nmis): int(ls[1])+nmis for ls in [line.strip().split() for line in infile] for nmis in range(opts.max5mis+1)}
    # Pdict = {(ls[0], nmis): ls[1] for ls in [line.strip().split() for line in infile] if opts.maxrdlen >= ls[0] >= opts.minrdlen
    #          for nmis in range(opts.max5mis+1)}
rdlens.sort()

gnd = HashedReadBAMGenomeArray(opts.bamfiles, ReadKeyMapFactory(Pdict, read_length_nmis))

# hash transcripts by ID for easy reference later
bedlinedict = {line.split()[3]: line for line in opts.inbed}
if not bedlinedict:
    raise EOFError('Insufficient input or empty file provided')

tfamtids = defaultdict(list)
with open('%s.txt' % opts.tfamstem, 'rU') as tfamtable:
    for line in tfamtable:
        ls = line.strip().split()
        tfamtids[ls[1]].append(ls[0])

with open('%s.bed' % opts.tfamstem, 'rU') as tfambed:
    tfambedlines = {line.split()[3]: line for line in tfambed}


def get_annotated_counts_by_chrom(chrom_to_do):
    found_CDSs = pd.read_hdf(opts.cdsstore, 'found_CDSs', mode='r', where="chrom == '%s'" % chrom_to_do, columns=['ORF_name']) \
        .merge(pd.read_hdf(opts.orfstore, 'all_ORFs', mode='r',
                           where="chrom == '%s' and tstop > 0 and tcoord > %d and AAlen > %d" % (chrom_to_do, -startnt[0], min_AAlen),
                           columns=['ORF_name', 'tfam', 'tid', 'tcoord', 'tstop', 'AAlen'])) \
        .sort('AAlen', ascending=False).drop_duplicates('tfam')  # use the longest annotated CDS in each transcript family
    num_cds_incl = 0  # number of CDSs included from this chromosome
    startprof = np.zeros((len(rdlens), startlen))
    cdsprof = np.zeros((len(rdlens), 3))
    stopprof = np.zeros((len(rdlens), stoplen))
    for (tid, tcoord, tstop) in found_CDSs[['tid', 'tcoord', 'tstop']].itertuples(False):
        curr_trans = Transcript.from_bed(bedlinedict[tid])
        tlen = curr_trans.get_length()
        if tlen >= tstop + stopnt[1]:  # need to guarantee that the 3' UTR is sufficiently long
            curr_hashed_counts = curr_trans.get_hashed_counts(gnd)
            cdslen = tstop+stopnt[1]-tcoord-stopnt[0]  # cds length, plus the extra bases...
            curr_counts = np.zeros((len(rdlens), cdslen))
            for (i, rdlen) in enumerate(rdlens):
                for nmis in range(opts.max5mis+1):
                    curr_counts[i, :] += curr_hashed_counts[(rdlen, nmis)][tcoord+stopnt[0]:tstop+stopnt[1]]
                    # curr_counts is limited to the CDS plus any extra requested nucleotides on either side
            if curr_counts.sum() >= opts.mincdsreads:
                curr_counts /= curr_counts.mean()  # normalize by mean of counts across all readlengths and positions within the CDS
                startprof += curr_counts[:, :startlen]
                cdsprof += curr_counts[:, startlen:cdslen-stoplen].reshape((len(rdlens), -1, 3)).mean(1)
                stopprof += curr_counts[:, cdslen-stoplen:cdslen]
                num_cds_incl += 1
    return startprof, cdsprof, stopprof, num_cds_incl

with pd.get_store(opts.orfstore, mode='r') as orfstore:
    chroms = orfstore.select('all_ORFs/meta/chrom/meta').values  # because saved as categorical, this is a list of all chromosomes

if opts.metagenefile and os.path.isfile(opts.metagenefile):
    import cPickle as pickle
    with open(opts.metagenefile, 'r') as infile:
        (startprof, cdsprof, stopprof) = pickle.load(infile)
else:
    startnt = (-abs(opts.startcodons[0])*3, abs(opts.startcodons[1])*3)  # demand <=0 and >= 0 for the bounds
    stopnt = (-abs(opts.stopcodons[0])*3, abs(opts.stopcodons[1])*3)
    min_AAlen = (startnt[1]-stopnt[0])/3  # actually should be longer than this to ensure at least one codon in the body
    startlen = startnt[1]-startnt[0]
    stoplen = stopnt[1]-stopnt[0]

    workers = mp.Pool(opts.numproc)
    (startprof, cdsprof, stopprof, num_cds_incl) = [sum(x) for x in workers.map(get_annotated_counts_by_chrom, chroms)]
    workers.close()

    startprof /= num_cds_incl
    cdsprof /= num_cds_incl
    stopprof /= num_cds_incl

    if opts.metagenefile:
        import cPickle as pickle
        with open(opts.metagenefile, 'wb') as outfile:
            pickle.dump((startprof, cdsprof, stopprof), outfile, -1)

for inbam in inbams:
    inbam.close()

