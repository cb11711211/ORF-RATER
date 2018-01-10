#! /usr/bin/env python

import argparse
import os
import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import GridSearchCV, cross_val_score, StratifiedKFold
from multiisotonic.multiisotonic import MultiIsotonicRegressor
import sys
from time import strftime

parser = argparse.ArgumentParser(description='Combine one or more output files from regress_orfs.py into a final translation rating for each ORF. '
                                             'Features will be loaded and calculated from the regression output, and scores will be calculated using '
                                             'a random forest, followed by a monotonization procedure to remove some overfitting artifacts.')
parser.add_argument('regressfile', nargs='+',
                    help='Subdirectory/subdirectories or filename(s) containing regression output from regress_orfs.py, for use in forming a final '
                         'rating. If directory(ies) are provided, they should contain a file named regression.h5. Datasets treated with translation '
                         'inititation inhibitors (e.g. HARR, LTM) for which the --startonly toggle was set in regress_orfs.py will only be used for '
                         'initiation codon results; other datasets will be used for both initiation and termination codons.')
parser.add_argument('--orfstore', default='orf.h5',
                    help='Path to pandas HDF store containing ORFs and ORF types; generated by find_orfs_and_types.py (Default: orf.h5)')
parser.add_argument('--names', nargs='+', help='Names to use for datasets included in REGRESSFILEs. Should meaningfully indicate the important '
                                               'features of each. (Default: inferred from REGRESSFILEs)')
parser.add_argument('--numtrees', type=int, default=2048, help='Number of trees to use in the random forest (Default: 2048)')
parser.add_argument('--minperleaf', type=int, nargs='+', default=[32],
                    help='Minimum samples per leaf to use in the random forest. If multiple values are provided, one will be selected based on cross '
                         'validation. If only one value is provided, will search for optimum by multiplying or dividing by powers of 2 (Default: 32)')
parser.add_argument('--minforestscore', type=float, default=0.3, help='Minimum forest score to require for monotonization (Default: 0.3)')
parser.add_argument('--cvfold', type=int, default=6, help='Number of folds for random forest cross-validation (Default: 6)')
parser.add_argument('--goldallcodons', action='store_true',
                    help='Random forest training set is normally restricted to ATG-initiated ORFs. If this flag is toggled, training will be '
                         'performed on all ORFs, which may unfairly penalize non-ATG-initiated ORFs.')
parser.add_argument('--goldminlen', type=int, default=100, help='Minimum length (in codons) for ORFs included in the training set (Default: 100)')
parser.add_argument('--ratingsfile', default='orfratings.h5',
                    help='Filename to which to output the final rating for each ORF. Formatted as pandas HDF (table name is "orfratings"). Columns '
                         'include basic information, raw score from random forest, and final monotonized orf rating. For ORFs appearing on multiple '
                         'transcripts, only one transcript will be selected for the table. (Default: orfratings.h5)')
parser.add_argument('--CSV', help='If included, also write output in CSV format to the provided filename.')
parser.add_argument('-v', '--verbose', action='store_true', help='Output a log of progress and timing (to stdout)')
parser.add_argument('-p', '--numproc', type=int, default=1, help='Number of processes to run. Defaults to 1 but more recommended if available.')
parser.add_argument('-f', '--force', action='store_true', help='Force file overwrite')
opts = parser.parse_args()

if not opts.force:
    if os.path.exists(opts.ratingsfile):
        raise IOError('%s exists; use --force to overwrite' % opts.ratingsfile)
    if opts.CSV and os.path.exists(opts.CSV):
        raise IOError('%s exists; use --force to overwrite' % opts.CSV)

regressfiles = []
colnames = []
for regressfile in opts.regressfile:
    if os.path.isfile(regressfile):
        regressfiles.append(regressfile)
        if not opts.names:
            colnames.append(os.path.basename(regressfile).rpartition(os.path.extsep)[0])  # '/path/to/myfile.h5' -> 'myfile'
    elif os.path.isdir(regressfile) and os.path.isfile(os.path.join(regressfile, 'regression.h5')):
        regressfiles.append(os.path.join(regressfile, 'regression.h5'))
        if not opts.names:
            colnames.append(os.path.basename(regressfile.strip(os.path.sep)))  # '/path/to/mydir/' -> 'mydir'
    else:
        raise IOError('Regression file/directory %s not found' % regressfile)

if opts.names:
    if len(opts.regressfile) != len(opts.names):
        raise ValueError('Precisely one name must be provided for each REGRESSFILE')
    colnames = opts.names

if opts.verbose:
    sys.stdout.write(' '.join(sys.argv) + '\n')

    def logprint(nextstr):
        sys.stdout.write('[%s] %s\n' % (strftime('%Y-%m-%d %H:%M:%S'), nextstr))
        sys.stdout.flush()

    logprint('Loading regression output')

orf_columns = ['orfname', 'tfam', 'tid', 'tcoord', 'tstop', 'chrom', 'gcoord', 'gstop', 'strand', 'codon', 'AAlen',
               'orftype', 'annot_start', 'annot_stop']
allstarts = pd.DataFrame(columns=['tfam', 'chrom', 'gcoord', 'strand'])
allorfs = pd.DataFrame()
allstops = pd.DataFrame(columns=['tfam', 'chrom', 'gstop', 'strand'])
feature_columns = []
stopcols = []
for (regressfile, colname) in zip(regressfiles, colnames):
    with pd.HDFStore(regressfile, mode='r') as instore:
        if 'stop_strengths' in instore:
            stopcols.append(colname)
            currstarts = instore.select('start_strengths', columns=['tfam', 'chrom', 'gcoord', 'strand', 'start_strength', 'W_start']) \
                .rename(columns={'start_strength': 'str_start_'+colname, 'W_start': 'W_start_'+colname})
            currstarts['chrom'] = currstarts['chrom'].astype(str)  # strange bug when merging on categorical columns, so revert to str temporarily
            currstarts['strand'] = currstarts['strand'].astype(str)
            allstarts = allstarts.merge(currstarts, how='outer').fillna(0.)

            allorfs = allorfs.append(instore.select('orf_strengths', columns=orf_columns), ignore_index=True).drop_duplicates('orfname')
            # This line not actually used for regression output beyond just which ORFs actually got a positive score in at least one regression
            # Safer to use concatenation and drop_duplicates rather than outer merges, in case one ORF somehow was assigned to different transcripts

            currstops = instore.select('stop_strengths', columns=['tfam', 'chrom', 'gstop', 'strand', 'stop_strength', 'W_stop']) \
                .rename(columns={'stop_strength': 'str_stop_'+colname, 'W_stop': 'W_stop_'+colname})
            currstops['chrom'] = currstops['chrom'].astype(str)  # strange bug when merging on categorical columns, so revert to str temporarily
            currstops['strand'] = currstops['strand'].astype(str)
            allstops = allstops.merge(currstops, how='outer').fillna(0.)

            feature_columns.extend(['W_start_'+colname, 'W_stop_'+colname, 'str_stop_'+colname])
        else:
            currstarts = instore.select('start_strengths', columns=['tfam', 'chrom', 'gcoord', 'strand', 'W_start']) \
                .rename(columns={'W_start': 'W_start_'+colname})
            currstarts['chrom'] = currstarts['chrom'].astype(str)  # strange bug when merging on categorical columns, so revert to str temporarily
            currstarts['strand'] = currstarts['strand'].astype(str)
            allstarts = allstarts.merge(currstarts, how='outer').fillna(0.)
            feature_columns.append('W_start_'+colname)

orfratings = allorfs[allorfs['gcoord'] != allorfs['gstop']].merge(allstarts, how='left').merge(allstops, how='left')
orfratings.fillna({col: 0. for col in orfratings.columns if col not in allorfs.columns}, inplace=True)

stopgrps = orfratings.groupby(['chrom', 'gstop', 'strand'])
for stopcol in stopcols:
    orfratings['stopset_rel_str_start_'+stopcol] = stopgrps['str_start_'+stopcol].transform(lambda x: x/x.max()).fillna(0.)
    feature_columns.append('stopset_rel_str_start_'+stopcol)

if opts.verbose:
    logprint('Training random forest on features:\n\t'+'\n\t'.join(feature_columns))

if opts.goldallcodons:
    gold_set = (orfratings['AAlen'] >= opts.goldminlen)
else:
    gold_set = ((orfratings['codon'] == 'ATG') & (orfratings['AAlen'] >= opts.goldminlen))
gold_df = orfratings[gold_set].drop_duplicates(['chrom', 'gcoord', 'gstop', 'strand'])
# ORFs with same start and stop are guaranteed to have identical feature values and therefore bias cross-validation - so only keep one of them
gold_class = gold_df[['annot_start', 'annot_stop']].all(1).values.astype(np.int8)*2 - 1  # convert True/False to +1/-1
gold_feat = gold_df[feature_columns].values

if opts.verbose:
    logprint('Gold set contains %d annotated ORFs and %d unannotated ORFs' % ((gold_class > 0).sum(), (gold_class < 0).sum()))

mycv = StratifiedKFold(opts.cvfold, shuffle=True, random_state=42)  # define random_state so same CV splits are used throughout parameter search
if len(opts.minperleaf) > 1:
    currgrid = GridSearchCV(RandomForestClassifier(n_estimators=opts.numtrees), param_grid={'min_samples_leaf': opts.minperleaf},
                            scoring='accuracy', cv=mycv, n_jobs=opts.numproc)
    currgrid.fit(gold_feat, gold_class)

    if opts.verbose:
        logprint('Best estimator has estimated %f accuracy with %d minimum samples per leaf' %
                 (currgrid.best_score_, currgrid.best_params_['min_samples_leaf']))

    if currgrid.best_params_['min_samples_leaf'] == min(opts.minperleaf) and min(opts.minperleaf) > 1:
        sys.stderr.write('WARNING: Optimal minimum samples per leaf is minimum tested; recommended to test lower values\n')
    if currgrid.best_params_['min_samples_leaf'] == max(opts.minperleaf):
        sys.stderr.write('WARNING: Optimal minimum samples per leaf is maximum tested; recommended to test greater values\n')

    best_est = currgrid.best_estimator_
else:
    def _get_score(val):
        return cross_val_score(RandomForestClassifier(n_estimators=opts.numtrees, min_samples_leaf=val),
                               gold_feat, gold_class, scoring='accuracy', cv=mycv, n_jobs=opts.numproc).mean()
    prevval = opts.minperleaf[0]
    prevres = _get_score(prevval)
    currval = prevval*2
    currres = _get_score(currval)
    if currres <= prevres:  # getting better as val decreases
        (prevval, currval) = (currval, prevval)
        (prevres, currres) = (currres, prevres)
        while currres >= prevres:
            if currval == 1:
                best_score = currres
                best_param = currval
                break
            prevval = currval
            prevres = currres
            currval //= 2
            currres = _get_score(currval)
        else:
            best_score = prevres
            best_param = prevval
    else:  # getting better as val increases
        while currres >= prevres:
            prevval = currval
            prevres = currres
            currval *= 2
            currres = _get_score(currval)
        best_score = prevres
        best_param = prevval
    if opts.verbose:
        logprint('Best estimator has estimated %f accuracy with %d minimum samples per leaf' % (best_score, best_param))

    best_est = RandomForestClassifier(n_estimators=opts.numtrees, min_samples_leaf=best_param, n_jobs=opts.numproc)
    best_est.fit(gold_feat, gold_class)

orfratings['forest_score'] = best_est.predict_proba(orfratings[feature_columns].values)[:, 1]

to_monotonize = orfratings['forest_score'] > opts.minforestscore

if opts.verbose:
    logprint('Monotonizing %d ORFs' % to_monotonize.sum())

forest_monoreg = MultiIsotonicRegressor()
forest_monoreg.fit(orfratings.loc[to_monotonize, feature_columns].values,
                   orfratings.loc[to_monotonize, 'forest_score'].values)

orfratings['orfrating'] = np.nan
orfratings.loc[to_monotonize, 'orfrating'] = forest_monoreg.predict(orfratings.loc[to_monotonize, feature_columns].values)

if opts.verbose:
    logprint('Saving results')

for catfield in ['chrom', 'strand', 'codon', 'orftype']:
    orfratings[catfield] = orfratings[catfield].astype('category')  # saves disk space and read/write time

orfratings.to_hdf(opts.ratingsfile, 'orfratings', format='t', data_columns=True)
if opts.CSV:
    orfratings.to_csv(opts.CSV, index=False)

if opts.verbose:
    logprint('Tasks complete')
