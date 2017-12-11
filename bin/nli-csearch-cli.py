#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys

import json
import pickle

import argparse

import numpy as np
import tensorflow as tf

from tensorflow.contrib import rnn
from tensorflow.contrib import legacy_seq2seq

from inferbeddings.nli import util, tfutil
from inferbeddings.nli import ConditionalBiLSTM
from inferbeddings.nli import FeedForwardDAM
from inferbeddings.nli import FeedForwardDAMP
from inferbeddings.nli import FeedForwardDAMS
from inferbeddings.nli import ESIMv1

import logging

logger = logging.getLogger(__name__)


entailment_idx, neutral_idx, contradiction_idx = 0, 1, 2

sentence1_ph = tf.placeholder(dtype=tf.int32, shape=[None, None], name='sentence1')
sentence2_ph = tf.placeholder(dtype=tf.int32, shape=[None, None], name='sentence2')

sentence1_len_ph = tf.placeholder(dtype=tf.int32, shape=[None], name='sentence1_length')
sentence2_len_ph = tf.placeholder(dtype=tf.int32, shape=[None], name='sentence2_length')

dropout_keep_prob_ph = tf.placeholder(tf.float32, name='dropout_keep_prob')

has_bos, has_eos, has_unk = True, False, True
is_lower = False

index_to_token = token_to_index = None

batch_size = 32


def main(argv):
    logger.info('Command line: {}'.format(' '.join(arg for arg in argv)))

    def fmt(prog):
        return argparse.HelpFormatter(prog, max_help_position=100, width=200)

    argparser = argparse.ArgumentParser('Regularising RTE via Adversarial Sets Regularisation', formatter_class=fmt)

    argparser.add_argument('--data', '-d', action='store', type=str, default='data/snli/snli_1.0_train.jsonl.gz')
    argparser.add_argument('--model', '-m', action='store', type=str, default='ff-dam',
                           choices=['cbilstm', 'ff-dam', 'ff-damp', 'ff-dams', 'esim1'])

    argparser.add_argument('--embedding-size', action='store', type=int, default=300)
    argparser.add_argument('--representation-size', action='store', type=int, default=200)

    argparser.add_argument('--batch-size', action='store', type=int, default=32)

    argparser.add_argument('--seed', action='store', type=int, default=0)

    argparser.add_argument('--has-bos', action='store_true', default=False, help='Has <Beginning Of Sentence> token')
    argparser.add_argument('--has-eos', action='store_true', default=False, help='Has <End Of Sentence> token')
    argparser.add_argument('--has-unk', action='store_true', default=False, help='Has <Unknown Word> token')
    argparser.add_argument('--lower', '-l', action='store_true', default=False, help='Lowercase the corpus')

    argparser.add_argument('--restore', action='store', type=str, default=None)
    argparser.add_argument('--lm', action='store', type=str, default='models/lm/')

    args = argparser.parse_args(argv)

    # Command line arguments
    data_path = args.data

    model_name = args.model

    embedding_size = args.embedding_size
    representation_size = args.representation_size

    batch_size = args.batch_size

    seed = args.seed

    has_bos = args.has_bos
    has_eos = args.has_eos
    has_unk = args.has_unk
    is_lower = args.lower

    restore_path = args.restore
    lm_path = args.lm

    logger.debug('Reading corpus ..')
    data_is, _, _ = util.SNLI.generate(train_path=data_path, valid_path=None, test_path=None, is_lower=is_lower)
    logger.info('Data size: {}'.format(len(data_is)))

    # Enumeration of tokens start at index=3:
    # index=0 PADDING, index=1 START_OF_SENTENCE, index=2 END_OF_SENTENCE, index=3 UNKNOWN_WORD
    bos_idx, eos_idx, unk_idx = 1, 2, 3

    global index_to_token, token_to_index
    with open('{}_index_to_token.p'.format(restore_path), 'rb') as f:
        index_to_token = pickle.load(f)

    index_to_token.update({0: '<PAD>', 1: '<BOS>', 2: '<UNK>'})

    token_to_index = {token: index for index, token in index_to_token.items()}

    with open('{}/config.json'.format(lm_path), 'r') as f:
        config = json.load(f)

    seq_length = 1
    lm_batch_size = batch_size
    rnn_size = config['rnn_size']
    num_layers = config['num_layers']

    label_to_index = {
        'entailment': entailment_idx,
        'neutral': neutral_idx,
        'contradiction': contradiction_idx,
    }

    max_len = None

    args = dict(
        has_bos=has_bos, has_eos=has_eos, has_unk=has_unk,
        bos_idx=bos_idx, eos_idx=eos_idx, unk_idx=unk_idx,
        max_len=max_len)

    dataset = util.instances_to_dataset(data_is, token_to_index, label_to_index, **args)

    sentence1, sentence1_length = dataset['sentence1'], dataset['sentence1_length']
    sentence2, sentence2_length = dataset['sentence2'], dataset['sentence2_length']
    label = dataset['label']

    clipped_sentence1 = tfutil.clip_sentence(sentence1_ph, sentence1_len_ph)
    clipped_sentence2 = tfutil.clip_sentence(sentence2_ph, sentence2_len_ph)

    vocab_size = max(token_to_index.values()) + 1

    discriminator_scope_name = 'discriminator'
    with tf.variable_scope(discriminator_scope_name):
        embedding_layer = tf.get_variable('embeddings', shape=[vocab_size, embedding_size], trainable=False)
        sentence1_embedding = tf.nn.embedding_lookup(embedding_layer, clipped_sentence1)
        sentence2_embedding = tf.nn.embedding_lookup(embedding_layer, clipped_sentence2)

        model_kwargs = dict(
            sequence1=sentence1_embedding, sequence1_length=sentence1_len_ph,
            sequence2=sentence2_embedding, sequence2_length=sentence2_len_ph,
            representation_size=representation_size, dropout_keep_prob=dropout_keep_prob_ph)

        if model_name in {'ff-dam', 'ff-damp', 'ff-dams'}:
            model_kwargs['init_std_dev'] = 0.01

        mode_name_to_class = {
            'cbilstm': ConditionalBiLSTM,
            'ff-dam': FeedForwardDAM,
            'ff-damp': FeedForwardDAMP,
            'ff-dams': FeedForwardDAMS,
            'esim1': ESIMv1
        }
        model_class = mode_name_to_class[model_name]
        assert model_class is not None

        model = model_class(**model_kwargs)
        logits = model()

        global probabilities
        probabilities = tf.nn.softmax(logits)

        predictions = tf.argmax(logits, axis=1, name='predictions')

    lm_scope_name = 'language_model'
    with tf.variable_scope(lm_scope_name):
        cell_fn = rnn.BasicLSTMCell
        cells = [cell_fn(rnn_size) for _ in range(num_layers)]

        global lm_cell
        lm_cell = rnn.MultiRNNCell(cells)

        global lm_input_data_ph, lm_targets_ph, lm_initial_state
        lm_input_data_ph = tf.placeholder(tf.int32, [None, seq_length], name='input_data')
        lm_targets_ph = tf.placeholder(tf.int32, [None, seq_length], name='targets')
        lm_initial_state = lm_cell.zero_state(lm_batch_size, tf.float32, )

        with tf.variable_scope('rnnlm'):
            lm_W = tf.get_variable(name='W', shape=[rnn_size, vocab_size],
                                   initializer=tf.contrib.layers.xavier_initializer())

            lm_b = tf.get_variable(name='b', shape=[vocab_size],
                                   initializer=tf.zeros_initializer())

            lm_emb_lookup = tf.nn.embedding_lookup(embedding_layer, lm_input_data_ph)
            lm_emb_projection = tf.contrib.layers.fully_connected(inputs=lm_emb_lookup, num_outputs=rnn_size,
                                                                  weights_initializer=tf.contrib.layers.xavier_initializer(),
                                                                  biases_initializer=tf.zeros_initializer())

            lm_inputs = tf.split(lm_emb_projection, seq_length, 1)
            lm_inputs = [tf.squeeze(input_, [1]) for input_ in lm_inputs]

        lm_outputs, lm_last_state = legacy_seq2seq.rnn_decoder(decoder_inputs=lm_inputs, initial_state=lm_initial_state,
                                                               cell=lm_cell, loop_function=None, scope='rnnlm')

        lm_output = tf.reshape(tf.concat(lm_outputs, 1), [-1, rnn_size])

        lm_logits = tf.matmul(lm_output, lm_W) + lm_b
        lm_probabilities = tf.nn.softmax(lm_logits)

        global lm_loss, lm_cost, lm_final_state
        lm_loss = legacy_seq2seq.sequence_loss_by_example(logits=[lm_logits], targets=[tf.reshape(lm_targets_ph, [-1])],
                                                          weights=[tf.ones([lm_batch_size * seq_length])])
        lm_cost = tf.reduce_sum(lm_loss) / lm_batch_size / seq_length
        lm_final_state = lm_last_state

    


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main(sys.argv[1:])
