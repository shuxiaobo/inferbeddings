#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import itertools
import os
import os.path

import sys
import argparse
import logging


def cartesian_product(dicts):
    return list(dict(zip(dicts, x)) for x in itertools.product(*dicts.values()))


def summary(configuration):
    kvs = sorted([(k, v) for k, v in configuration.items()], key=lambda e: e[0])
    return '_'.join([('%s=%s' % (k, v)) for (k, v) in kvs])


def to_cmd(c, _path=None):
    if _path is None:
        _path = '/home/pminervi/workspace/inferbeddings/'
    unit_cube_str = '--unit-cube' if c['unit_cube'] else ''
    loss_str = ''
    if c['loss'] == 'hinge':
        loss_str = '--loss hinge'
    elif c['loss'] == 'pairwise_hinge':
        loss_str = '--pairwise-loss hinge'
    assert loss_str is not None
    command = 'python3 {}/bin/adv-cli.py' \
              ' --train {}/data/music_mte10_5k/music_mte10-train.tsv' \
              ' --valid {}/data/music_mte10_5k/music_mte10-valid.tsv' \
              ' --test {}/data/music_mte10_5k/music_mte10-test.tsv' \
              ' --clauses {}/data/music_mte10_5k/clauses/clauses_equivalencies.pl' \
              ' --nb-epochs {}' \
              ' --nb-batches 10' \
              ' --model {}' \
              ' --similarity {}' \
              ' --margin {}' \
              ' --embedding-size {}' \
              ' {} {} --sar-weight {} --sar-similarity {}' \
              ''.format(_path, _path, _path, _path, _path,
                        c['epochs'],
                        c['model'], c['similarity'],
                        c['margin'], c['embedding_size'],
                        loss_str, unit_cube_str, c['sar_weight'], c['sar_similarity'])
    return command


def to_logfile(c, path):
    outfile = "%s/ucl_music_SAR_v1.%s.log" % (path, summary(c))
    return outfile


def main(argv):
    def formatter(prog):
        return argparse.HelpFormatter(prog, max_help_position=100, width=200)

    argparser = argparse.ArgumentParser('Generating experiments for the UCL cluster', formatter_class=formatter)
    argparser.add_argument('--debug', '-D', action='store_true', help='Debug flag')
    argparser.add_argument('--path', '-p', action='store', type=str, default=None, help='Path')

    args = argparser.parse_args(argv)

    hyperparameters_space_1 = dict(
        epochs=[1000],
        model=['DistMult', 'ComplEx'],
        similarity=['dot'],
        margin=[1],  # margin=[1, 2, 5, 10],
        embedding_size=[20, 50, 100, 150, 200],
        unit_cube=[True, False],
        sar_weight=[0, .01, 1, 100, 10000, 1000000],
        sar_similarity=['l2_sqr'],  # sar_similarity=['dot', 'l1', 'l2', 'l2_sqr'],
        loss=['pairwise_hinge']  # loss=['hinge', 'pairwise_hinge'],
    )

    hyperparameters_space_2 = dict(
        epochs=[1000],
        model=['TransE'],
        similarity=['l1', 'l2'],
        margin=[1],  # margin=[1, 2, 5, 10],
        embedding_size=[20, 50, 100, 150, 200],
        unit_cube=[True, False],
        sar_weight=[0, .01, 1, 100, 10000, 1000000],
        sar_similarity=['l2_sqr'],  # sar_similarity=['dot', 'l1', 'l2', 'l2_sqr'],
        loss=['hinge']  #  loss=['hinge', 'pairwise_hinge'],
    )

    configurations = cartesian_product(hyperparameters_space_1) + cartesian_product(hyperparameters_space_2)

    path = '/home/pminervi/workspace/inferbeddings/logs/sar/ucl_music_SAR_v1/'

    # Check that we are on the UCLCS cluster first
    if os.path.exists('/home/pminervi/'):
        # If the folder that will contain logs does not exist, create it
        if not os.path.exists(path):
            os.makedirs(path)

    configurations = list(configurations)

    command_lines = set()
    for cfg in configurations:
        logfile = to_logfile(cfg, path)

        completed = False
        if os.path.isfile(logfile):
            with open(logfile, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                completed = '### MICRO (test filtered)' in content

        if not completed:
            command_line = '{} >> {} 2>&1'.format(to_cmd(cfg, _path=args.path), logfile)
            command_lines |= {command_line}

    # Sort command lines and remove duplicates
    sorted_command_lines = sorted(command_lines)
    nb_jobs = len(sorted_command_lines)

    header = """#!/bin/bash

#$ -cwd
#$ -S /bin/bash
#$ -o /dev/null
#$ -e /dev/null
#$ -t 1-{}
#$ -l h_vmem=6G,tmem=6G
#$ -l h_rt=4:00:00

export LANG="en_GB.UTF-8"

""".format(nb_jobs)

    print(header)

    for job_id, command_line in enumerate(sorted_command_lines, 1):
        print('test $SGE_TASK_ID -eq {} && {}'.format(job_id, command_line))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main(sys.argv[1:])