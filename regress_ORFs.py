#! /usr/bin/env python

import argparse
import sys
import os
import pysam
from collections import defaultdict
import pandas as pd
import numpy as np
from scipy.optimize import nnls
import scipy.sparse
import multiprocessing as mp
from yeti.genomics.genome_array import HashedReadBAMGenomeArray, ReadKeyMapFactory, read_length_nmis
from yeti.genomics.roitools import SegmentChain, positionlist_to_segments

parser = argparse.ArgumentParser()
parser.add_argument('orfstore', help='Path to pandas HDF store containing ORFs to regress; generated by find_ORFs.py')
parser.add_argument('cdsstore', help='Path to pandas HDF store containing CDS information; generated by find_annotated_CDSs.py')
parser.add_argument('tfamstem', help='Transcript family information generated by make_tfams.py. '
                                     'Both TFAMSTEM.txt and TFAMSTEM.bed should exist or an error will result.')
parser.add_argument('offset', help='Path to 2-column tab-delimited file with 5\' offsets for variable P-site mappings. First column indicates read '
                                   'length, second column indicates offset to apply. Read lengths are calculated after trimming 5\' mismatches. '
                                   'Accepted read lengths are defined by those present in the first column of this file.')
parser.add_argument('--outfile', help='Filename to which to output the table of regression scores for each ORF. Formatted as pandas HDF (preferred '
                                      'extension is .h5; tables generated include "start_strengths", "ORF_strengths", and "stop_strengths"). If not '
                                      'provided, the program will generate a metagene profile and quit (i.e. no regression will be performed).')
parser.add_argument('bamfiles', nargs='+', help='Path to transcriptome-aligned BAM file(s) for read data')
parser.add_argument('--inbed', type=argparse.FileType('rU'), default=sys.stdin, help='Transcriptome BED-file (Default: stdin)')
parser.add_argument('--startonly', action='store_true', help='Toggle for datasets collected in the presence of initiation inhibitor (e.g. Harr, '
                                                             'LTM). If selected, only "start_strengths" will be calculated and saved.')
parser.add_argument('--startrange', type=int, nargs=2, default=[1, 50],
                    help='Region around start codon (in codons) to model explicitly. Ignored if reading metagene from file (Default: 1 30, meaning '
                         'one full codon before the start is modeled, as are the start codon and the 49 codons following it).')
parser.add_argument('--stoprange', type=int, nargs=2, default=[7, 0],
                    help='Region around stop codon (in codons) to model explicitly. Ignored if reading metagene from file (Default: 7 0, meaning '
                         'seven full codons before and including the stop are modeled, but none after).')
parser.add_argument('--mincdsreads', type=int, default=64,
                    help='Minimum number of reads required within the body of the CDS (and any surrounding nucleotides indicated by STARTRANGE or '
                         'STOPRANGE) for it to be included in the metagene. Ignored if reading metagene from file (Default: 64).')
parser.add_argument('--startcount', type=int, default=0,
                    help='Minimum reads at putative translation initiation codon. Useful to reduce computational burden by only considering ORFs '
                         'with e.g. at least 1 read at the start. (Default: 0)')
# parser.add_argument('--minrdlen', type=int, default=27, help='Minimum permitted read length')
# parser.add_argument('--maxrdlen', type=int, default=34, help='Maximum permitted read length (inclusive)')
# min and max read length inferred from the offset file
parser.add_argument('--max5mis', type=int, default=1, help='Maximum 5\' mismatches to trim. Reads with more than this number will be excluded.'
                                                           '(Default: 1)')
parser.add_argument('--metagenefile', help='File to save metagene profile, OR if the file already exists, it will be used as the input metagene. '
                                           'Formatted as tab-delimited text, with position, readlength, value, and type ("START", "CDS", or "STOP").')
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

gnd = HashedReadBAMGenomeArray(inbams, ReadKeyMapFactory(Pdict, read_length_nmis))

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
        curr_trans = SegmentChain.from_bed(bedlinedict[tid])
        tlen = curr_trans.get_length()
        if tlen >= tstop + stopnt[1]:  # need to guarantee that the 3' UTR is sufficiently long
            curr_hashed_counts = curr_trans.get_hashed_counts(gnd)
            cdslen = tstop+stopnt[1]-tcoord-startnt[0]  # cds length, plus the extra bases...
            curr_counts = np.zeros((len(rdlens), cdslen))
            for (i, rdlen) in enumerate(rdlens):
                for nmis in range(opts.max5mis+1):
                    curr_counts[i, :] += curr_hashed_counts[(rdlen, nmis)][tcoord+startnt[0]:tstop+stopnt[1]]
                    # curr_counts is limited to the CDS plus any extra requested nucleotides on either side
            if curr_counts.sum() >= opts.mincdsreads:
                curr_counts /= curr_counts.mean()  # normalize by mean of counts across all readlengths and positions within the CDS
                startprof += curr_counts[:, :startlen]
                cdsprof += curr_counts[:, startlen:cdslen-stoplen].reshape((len(rdlens), -1, 3)).mean(1)
                stopprof += curr_counts[:, cdslen-stoplen:cdslen]
                num_cds_incl += 1
    return startprof, cdsprof, stopprof, num_cds_incl


def ORF_profile(orflen):
    """Generate a profile for an ORF based on the metagene profile
    Parameters
    ----------
    orflen : int
        Number of nucleotides in the ORF, including the start and stop codons

    Returns
    -------
    np.ndarray<float>
        The expected profile for the ORF. Number of rows will match the number of rows in the metagene profile. Number of columns will be
        orflen + stopnt[1] - startnt[0]
    """
    assert orflen % 3 == 0
    assert orflen > 0
    short_stop = 9
    if orflen >= startnt[1]-stopnt[0]:  # long enough to include everything
        return np.hstack((startprof, np.tile(cdsprof, (orflen-startnt[1]+stopnt[0])/3), stopprof))
    elif orflen >= startnt[1]+short_stop:
        return np.hstack((startprof, stopprof[:, startnt[1]-orflen-stopnt[1]:]))
    elif orflen >= short_stop:
        return np.hstack((startprof[:, :orflen-short_stop-startnt[0]], stopprof[:, -short_stop-stopnt[1]:]))
    else:  # very short!
        return np.hstack((startprof[:, :3-startnt[0]], stopprof[:, 3-orflen-stopnt[0]:]))


if opts.startonly:
    failure_return = (pd.DataFrame(), pd.DataFrame())
else:
    failure_return = (pd.DataFrame(), pd.DataFrame(), pd.DataFrame())


def regress_tfam(ORF_set):
    tfam = ORF_set['tfam'].iat[0]
    strand = ORF_set['strand'].iat[0]
    chrom = ORF_set['chrom'].iat[0]
    # currtfam = SegmentChain.from_bed(tfambedlines[ORF_set['tfam'].iat[0]])
    # all_tfam_genpos = currtfam.get_position_list(stranded=True)
    tids = ORF_set['tid'].drop_duplicates().tolist()
    all_tfam_genpos = set()
    tid_genpos = {}
    tlens = {}
    for (i, tid) in enumerate(tids):
        currtrans = SegmentChain.from_bed(bedlinedict[tid])
        curr_pos_set = currtrans.get_position_set()
        tlens[tid] = len(curr_pos_set)
        tid_genpos[tid] = curr_pos_set
        all_tfam_genpos.update(curr_pos_set)
    tfam_segs = SegmentChain(*positionlist_to_segments(chrom, strand, all_tfam_genpos))
    tfam_segs.get_position_list(stranded=True)
    all_tfam_genpos = np.array(sorted(all_tfam_genpos))
    if strand == '-':
        all_tfam_genpos = all_tfam_genpos[::-1]
    nnt = len(all_tfam_genpos)
    tid_indices = {tid: np.flatnonzero(np.in1d(all_tfam_genpos, list(curr_tid_genpos), assume_unique=True))
                   for (tid, curr_tid_genpos) in tid_genpos.iteritems()}
    hashed_counts = tfam_segs.get_hashed_counts(gnd)
    counts = np.zeros((len(rdlens), nnt), dtype=np.float64)  # even though they are integer-valued, will need to do float arithmetic
    for (i, rdlen) in enumerate(rdlens):
        for nmis in range(1+opts.max5mis):
            counts[i, :] += hashed_counts[(rdlen, nmis)]
    counts = counts.ravel()

    if opts.startcount:
        # Only include ORFS for which there is at least some minimum reads within one nucleotide of the start codon
        offsetmat = np.tile(nnt*np.arange(len(rdlens)), 3)  # offsets for each cond, expecting three positions to check for each
    #    try:
        ORF_set = ORF_set[[(counts[(start_idxes.repeat(len(rdlens))+offsetmat)].sum() >= opts.startcount) for start_idxes in
                           [tid_indices[tid][tcoord-1:tcoord+2] for (tid, tcoord, tstop) in ORF_set[['tid', 'tcoord', 'tstop']].itertuples(False)]]]
        if ORF_set.empty:
            return failure_return

    ORF_strength_df = ORF_set.sort('tcoord', ascending=False).drop_duplicates('ORF_name').reset_index()
    abort_set = ORF_set.drop_duplicates('gcoord').copy()
    abort_set['gstop'] = abort_set['gcoord']  # should maybe be +/-3, but then need to worry about splicing - and this is an easy flag
    abort_set['tstop'] = abort_set['tcoord']+3  # stop after the first codon
    abort_set['ORF_name'] = abort_set['gcoord'].apply(lambda x: '%s_%d_abort' % (tfam, x))
    ORF_strength_df = pd.concat((ORF_strength_df, abort_set), ignore_index=True)
    if not opts.startonly:  # if marking full ORFs, include histop model
        stop_set = ORF_set.drop_duplicates('gstop').copy()
        stop_set['gcoord'] = stop_set['gstop']  # this is an easy flag
        stop_set['tcoord'] = stop_set['tstop']  # should probably be -3 nt, but this is another easy flag that distinguishes from abinit
        stop_set['ORF_name'] = stop_set['gstop'].apply(lambda x: '%s_%d_stop' % (tfam, x))
        ORF_strength_df = pd.concat((ORF_strength_df, stop_set), ignore_index=True)
    nORF = len(ORF_strength_df)  # inclusive of abortive initiation and histop events
    ORF_profs = []
    indices = []
    for (ORF_num, tid, tcoord, tstop) in ORF_strength_df[['tid', 'tcoord', 'tstop']].itertuples(True):  # index is 0..nORF
        if tcoord != tstop:  # not a histop
            tlen = tlens[tid]
            if tcoord+startnt[0] < 0:
                startadj = -startnt[0]-tcoord  # number of nts to remove from the start due to short 5' UTR; guaranteed > 0
            else:
                startadj = 0
            if tstop+stopnt[1] > tlen:
                stopadj = tstop+stopnt[1]-tlen  # number of nts to remove from the end due to short 3' UTR; guaranteed > 0
            else:
                stopadj = 0
            curr_indices = tid_indices[tid][tcoord+startnt[0]+startadj:tstop+stopnt[1]-stopadj]
            ORF_profs.append(ORF_profile(tstop-tcoord)[:, startadj:tstop-tcoord+stopnt[1]-startnt[0]-stopadj].ravel())
        else:  # histop
            curr_indices = tid_indices[tid][tstop-6:tstop]
            ORF_profs.append(stopprof[:, -6:].ravel())
        indices.append(np.concatenate([nnt*i+curr_indices for i in xrange(len(rdlens))]))
        # need to tile the indices for each read length
        if len(indices[-1]) != len(ORF_profs[-1]):
            raise AssertionError('ORF length does not match index length')
    ORF_matrix = scipy.sparse.csc_matrix((np.concatenate(ORF_profs),
                                          np.concatenate(indices),
                                          np.cumsum([0]+[len(curr_indices) for curr_indices in indices])),
                                         shape=(nnt*len(rdlens), nORF))
    # better to make it a sparse matrix, even though nnls requires a dense matrix, because of linear algebra to come
    nonzero_ORFs = np.flatnonzero(ORF_matrix.T.dot(counts) > 0)
    if len(nonzero_ORFs) == 0:  # no possibility of anything coming up
        return failure_return
    ORF_matrix = ORF_matrix[:, nonzero_ORFs]
    ORF_strength_df = ORF_strength_df.iloc[nonzero_ORFs]  # don't bother fitting ORFs with zero reads throughout their entire length
    nORF = len(nonzero_ORFs)
    (ORF_strs, resid) = nnls(ORF_matrix.toarray(), counts)
    min_str = 1e-6  # allow for machine rounding error
    usable_ORFs = ORF_strs > min_str
    if not usable_ORFs.any():
        return failure_return
    ORF_strength_df = ORF_strength_df[usable_ORFs]
    ORF_matrix = ORF_matrix[:, usable_ORFs] # remove entries for zero-strength ORFs or transcripts
    ORF_strs = ORF_strs[usable_ORFs]
    ORF_strength_df['ORF_strength'] = ORF_strs

    covmat = resid*resid*np.linalg.inv(ORF_matrix.T.dot(ORF_matrix).toarray())/(nnt*len(rdlens)-len(ORF_strength_df))
    # homoscedastic version (assume equal variance at all positions)

    # resids = counts-ORF_matrix.dot(ORF_strs)
    # simple_covmat = np.linalg.inv(ORF_matrix.T.dot(ORF_matrix).toarray())
    # covmat = simple_covmat.dot(ORF_matrix.T.dot(scipy.sparse.dia_matrix((resids*resids, 0), (len(resids), len(resids))))
    #                            .dot(ORF_matrix).dot(simple_covmat))
    # # heteroscedastic version (Eicker-Huber-White robust estimator)

    ORF_strength_df['W_ORF'] = ORF_strength_df['ORF_strength']*ORF_strength_df['ORF_strength']/np.diag(covmat)
    ORF_strength_df.set_index('ORF_name', inplace=True)
    elongating_ORFs = ~(ORF_strength_df['gstop'] == ORF_strength_df['gcoord'])
    if opts.startonly:  # count abortive initiation events towards start strength in this case
        include_starts = (ORF_strength_df['tcoord'] != ORF_strength_df['tstop'])
        gcoord_grps = ORF_strength_df[include_starts].groupby('gcoord')
        # even if we are willing to count abinit towards start strength, we certainly shouldn't count histop
        covmat_starts = covmat[np.ix_(include_starts.values, include_starts.values)]
        ORF_strs_starts = ORF_strs[include_starts.values]
    else:
        gcoord_grps = ORF_strength_df[elongating_ORFs].groupby('gcoord')
        covmat_starts = covmat[np.ix_(elongating_ORFs.values, elongating_ORFs.values)]
        ORF_strs_starts = ORF_strs[elongating_ORFs.values]
    start_strength_df = pd.DataFrame.from_items([('tfam', tfam),
                                                 ('chrom', ORF_set['chrom'].iloc[0]),
                                                 ('strand', ORF_set['strand'].iloc[0]),
                                                 ('codon', gcoord_grps['codon'].first()),
                                                 ('start_strength', gcoord_grps['ORF_strength'].aggregate(np.sum))])
    start_strength_df['W_start'] = pd.Series({gcoord: ORF_strs_starts[rownums].dot(np.linalg.inv(covmat_starts[np.ix_(rownums, rownums)]))
                                              .dot(ORF_strs_starts[rownums]) for (gcoord, rownums) in gcoord_grps.indices.iteritems()})

    if not opts.startonly:
        # count histop towards the stop codon - but still exclude abinit
        include_stops = (elongating_ORFs | (ORF_strength_df['tcoord'] == ORF_strength_df['tstop']))
        gstop_grps = ORF_strength_df[include_stops].groupby('gstop')
        covmat_stops = covmat[np.ix_(include_stops.values, include_stops.values)]
        ORF_strs_stops = ORF_strs[include_stops.values]
        stop_strength_df = pd.DataFrame.from_items([('tfam', tfam),
                                                    ('chrom', ORF_set['chrom'].iloc[0]),
                                                    ('strand', ORF_set['strand'].iloc[0]),
                                                    ('stop_strength', gstop_grps['ORF_strength'].aggregate(np.sum))])
        stop_strength_df['W_stop'] = pd.Series({gstop: ORF_strs_stops[rownums].dot(np.linalg.inv(covmat_stops[np.ix_(rownums, rownums)]))
                                                .dot(ORF_strs_stops[rownums]) for (gstop, rownums) in gstop_grps.indices.iteritems()})

        # # then do nohistop
        # gstop_grps = ORF_strength_df[elongating_ORFs].groupby('gstop')
        # covmat_stops = covmat[np.ix_(elongating_ORFs.values, elongating_ORFs.values)]
        # ORF_strs_stops = ORF_strs[elongating_ORFs.values]
        # stop_strength_df['stop_strength_nohistop'] = gstop_grps['ORF_strength'].aggregate(np.sum)
        # stop_strength_df['W_stop_nohistop'] = pd.Series({gstop:ORF_strs_stops[rownums].dot(np.linalg.inv(covmat_stops[np.ix_(rownums,rownums)]))
        #                                                  .dot(ORF_strs_stops[rownums]) for (gstop, rownums) in gstop_grps.indices.iteritems()})

        return ORF_strength_df, start_strength_df, stop_strength_df
    else:
        return ORF_strength_df, start_strength_df


def regress_chrom(chrom_to_do):
    chrom_ORFs = pd.read_hdf(opts.orfstore, 'all_ORFs', mode='r', where="chrom == '%s' and tstop > 0 and tcoord > 0" % chrom_to_do,
                             columns=['ORF_name', 'tfam', 'tid', 'tcoord', 'tstop', 'AAlen', 'chrom', 'gcoord', 'gstop', 'strand', 'codon'])
    # tcoord > 0 removes ORFs where the first codon is an NTG, to avoid an indexing error
    # Those ORFs would never get called anyway since they couldn't possibly have any reads at their start codon
    if chrom_ORFs.empty:
        return failure_return

    return [pd.concat(res_dfs) for res_dfs in zip(*[regress_tfam(tfam_set) for (tfam, tfam_set) in chrom_ORFs.groupby('tfam')])]

with pd.get_store(opts.orfstore, mode='r') as orfstore:
    chroms = orfstore.select('all_ORFs/meta/chrom/meta').values  # because saved as categorical, this is the list of all chromosomes

if opts.metagenefile and os.path.isfile(opts.metagenefile):
    metagene = pd.read_csv(opts.metagenefile, sep='\t').set_index(['region', 'position'])
    metagene.columns = metagene.columns.astype(int)  # they are read lengths
    assert (metagene.columns == rdlens).all()
    startprof = metagene.loc['START']
    cdsprof = metagene.loc['CDS']
    stopprof = metagene.loc['STOP']
    startnt = (startprof.index.min(), startprof.index.max()+1)
    assert len(cdsprof) == 3
    stopnt = (stopprof.index.min(), stopprof.index.max()+1)
    startprof = startprof.values.T
    cdsprof = cdsprof.values.T
    stopprof = stopprof.values.T
else:
    startnt = (-abs(opts.startrange[0])*3, abs(opts.startrange[1])*3)  # force <=0 and >= 0 for the bounds
    stopnt = (-abs(opts.stoprange[0])*3, abs(opts.stoprange[1])*3)

    if stopnt[0] >= -6:
        raise ValueError('STOPRANGE must encompass at least 3 codons prior to the stop')
    min_AAlen = (startnt[1]-stopnt[0])/3  # actually should be longer than this to ensure at least one codon in the body
    startlen = startnt[1]-startnt[0]
    stoplen = stopnt[1]-stopnt[0]

    workers = mp.Pool(opts.numproc)
    (startprof, cdsprof, stopprof, num_cds_incl) = [sum(x) for x in zip(*workers.map(get_annotated_counts_by_chrom, chroms))]
    workers.close()

    startprof /= num_cds_incl  # technically not necessary, but helps for consistency of units
    cdsprof /= num_cds_incl
    stopprof /= num_cds_incl

    if opts.metagenefile:
        pd.concat((pd.DataFrame(data=startprof.T,
                                index=pd.MultiIndex.from_product(['START', np.arange(*startnt)], names=['region', 'position']),
                                columns=pd.Index(rdlens, name='rdlen')),
                   pd.DataFrame(data=cdsprof.T,
                                index=pd.MultiIndex.from_product(['CDS', np.arange(3)], names=['region', 'position']),
                                columns=pd.Index(rdlens, name='rdlen')),
                   pd.DataFrame(data=stopprof.T,
                                index=pd.MultiIndex.from_product(['STOP', np.arange(*stopnt)], names=['region', 'position']),
                                columns=pd.Index(rdlens, name='rdlen')))) \
            .to_csv(opts.metagenefile, sep='\t')

if opts.outfile:
    workers = mp.Pool(opts.numproc)
    if opts.startonly:
        (ORF_strengths, start_strengths) = \
            [pd.concat(res_dfs).reset_index() for res_dfs in zip(*workers.map(regress_chrom, chroms))]
        with pd.get_store(opts.outfile, mode='w') as outstore:
            outstore.put('ORF_strengths', ORF_strengths, format='t', data_columns=True)
            outstore.put('start_strengths', start_strengths, format='t', data_columns=True)
    else:
        (ORF_strengths, start_strengths, stop_strengths) = \
            [pd.concat(res_dfs).reset_index() for res_dfs in zip(*workers.map(regress_chrom, chroms))]
        with pd.get_store(opts.outfile, mode='w') as outstore:
            outstore.put('ORF_strengths', ORF_strengths, format='t', data_columns=True)
            outstore.put('start_strengths', start_strengths, format='t', data_columns=True)
            outstore.put('stop_strengths', stop_strengths, format='t', data_columns=True)
    workers.close()

for inbam in inbams:
    inbam.close()
