# -*- coding: utf-8 -*-

from abc import abstractmethod

import numpy as np
import tensorflow as tf

from inferbeddings.rte2 import util
from inferbeddings.rte2.base import BaseRTEModel

import logging

logger = logging.getLogger(__name__)


class BaseESIM(BaseRTEModel):
    @abstractmethod
    def _transform_input(self, sequence, sequence_length, reuse=False):
        raise NotImplementedError

    @abstractmethod
    def _transform_attend(self, sequence, reuse=False):
        raise NotImplementedError

    @abstractmethod
    def _transform_compare(self, sequence, reuse=False):
        raise NotImplementedError

    @abstractmethod
    def _transform_aggregate(self, v1_v2, reuse=False):
        raise NotImplementedError

    def __init__(self, use_masking=False, *args, **kwargs):
        super().__init__(*args, **kwargs)

        embedding1_size = self.sequence1.get_shape()[-1].value
        embedding2_size = self.sequence2.get_shape()[-1].value

        assert embedding1_size == embedding2_size

        # [batch_size, time_steps, embedding_size] -> [batch_size, time_steps, representation_size]
        self.transformed_sequence1 = self._transform_input(self.sequence1, self.sequence1_length, reuse=self.reuse)

        # [batch_size, time_steps, embedding_size] -> [batch_size, time_steps, representation_size]
        self.transformed_sequence2 = self._transform_input(self.sequence2, self.sequence2_length, reuse=True)

        sequence1 = self.transformed_sequence1
        sequence2 = self.transformed_sequence2

        sequence1_length = self.sequence1_length
        sequence2_length = self.sequence2_length

        logger.info('Building the Attend graph ..')

        self.raw_attentions = None
        self.attention_sentence1 = self.attention_sentence2 = None

        # tensors with shape (batch_size, time_steps, num_units)
        self.alpha, self.beta = self.attend(sequence1=sequence1, sequence2=sequence2,
                                            sequence1_lengths=sequence1_length, sequence2_lengths=sequence2_length,
                                            use_masking=use_masking, reuse=self.reuse)

        logger.info('Building the Compare graph ..')

        # tensor with shape (batch_size, time_steps, num_units)
        self.v1 = self.compare(sequence1, self.beta, reuse=self.reuse)

        # tensor with shape (batch_size, time_steps, num_units)
        self.v2 = self.compare(sequence2, self.alpha, reuse=True)

        logger.info('Building the Aggregate graph ..')
        self.logits = self.aggregate(self.v1, self.v2, self.nb_classes,
                                     v1_lengths=sequence1_length, v2_lengths=sequence2_length,
                                     use_masking=use_masking, reuse=self.reuse)

    def __call__(self):
            return self.logits

    def attend(self, sequence1, sequence2,
               sequence1_lengths=None, sequence2_lengths=None, use_masking=False, reuse=False):
        """
        Attend phase.

        :param sequence1: tensor with shape (batch_size, time_steps, num_units)
        :param sequence2: tensor with shape (batch_size, time_steps, num_units)
        :param sequence1_lengths: time_steps in sequence1
        :param sequence2_lengths: time_steps in sequence2
        :param use_masking: use masking
        :param reuse: reuse variables
        :return: two tensors with shape (batch_size, time_steps, num_units)
        """
        with tf.variable_scope('attend') as _:
            # tensor with shape (batch_size, time_steps, num_units)
            transformed_sequence1 = self._transform_attend(sequence1, reuse=reuse)

            # tensor with shape (batch_size, time_steps, num_units)
            transformed_sequence2 = self._transform_attend(sequence2, reuse=True)

            # tensor with shape (batch_size, time_steps, time_steps)
            tmp = tf.transpose(transformed_sequence2, [0, 2, 1])
            self.raw_attentions = tf.matmul(transformed_sequence1, tmp)

            masked_raw_attentions = self.raw_attentions
            if use_masking:
                masked_raw_attentions = util.mask_3d(sequences=masked_raw_attentions,
                                                     sequence_lengths=sequence2_lengths,
                                                     mask_value=- np.inf, dimension=2)
            self.attention_sentence1 = util.attention_softmax3d(masked_raw_attentions)

            # tensor with shape (batch_size, time_steps, time_steps)
            attention_transposed = tf.transpose(self.raw_attentions, [0, 2, 1])
            masked_attention_transposed = attention_transposed
            if use_masking:
                masked_attention_transposed = util.mask_3d(sequences=masked_attention_transposed,
                                                           sequence_lengths=sequence1_lengths,
                                                           mask_value=- np.inf, dimension=2)
            self.attention_sentence2 = util.attention_softmax3d(masked_attention_transposed)

            # tensors with shape (batch_size, time_steps, num_units)
            alpha = tf.matmul(self.attention_sentence2, sequence1, name='alpha')
            beta = tf.matmul(self.attention_sentence1, sequence2, name='beta')
            return alpha, beta

    def compare(self, sentence, soft_alignment, reuse=False):
        """
        Compare phase.

        :param sentence: tensor with shape (batch_size, time_steps, num_units)
        :param soft_alignment: tensor with shape (batch_size, time_steps, num_units)
        :param reuse: reuse variables
        :return: tensor with shape (batch_size, time_steps, num_units)
        """
        # tensor with shape (batch, time_steps, num_units)
        values = [sentence, soft_alignment, sentence - soft_alignment, sentence * soft_alignment]
        sentence_and_alignment = tf.concat(axis=2, values=values)
        transformed_sentence_and_alignment = self._transform_compare(sentence_and_alignment, reuse=reuse)
        return transformed_sentence_and_alignment

    def aggregate(self, v1, v2, num_classes,
                  v1_lengths=None, v2_lengths=None, use_masking=False, reuse=False):
        """
        Aggregate phase.

        :param v1: tensor with shape (batch_size, time_steps, num_units)
        :param v2: tensor with shape (batch_size, time_steps, num_units)
        :param num_classes: number of output units
        :param v1_lengths: time_steps in v1
        :param v2_lengths: time_steps in v2
        :param use_masking: use masking
        :param reuse: reuse variables
        :return: 
        """
        with tf.variable_scope('aggregate', reuse=reuse) as _:
            if use_masking:
                v1 = util.mask_3d(sequences=v1, sequence_lengths=v1_lengths, mask_value=0, dimension=1)
                v2 = util.mask_3d(sequences=v2, sequence_lengths=v2_lengths, mask_value=0, dimension=1)

            v1_mean, v2_mean = tf.reduce_mean(v1, [1]), tf.reduce_mean(v2, [1])
            v1_min, v2_min = tf.reduce_min(v1, [1]), tf.reduce_min(v2, [1])
            v1_max, v2_max = tf.reduce_max(v1, [1]), tf.reduce_max(v2, [1])

            v1_v2 = tf.concat(axis=1, values=[v1_mean, v1_min, v1_max, v2_mean, v2_min, v2_max])
            transformed_v1_v2 = self._transform_aggregate(v1_v2, reuse=reuse)

            logits = tf.contrib.layers.fully_connected(inputs=transformed_v1_v2,
                                                       num_outputs=num_classes,
                                                       weights_initializer=tf.random_normal_initializer(0.0, 0.01),
                                                       biases_initializer=tf.zeros_initializer(),
                                                       activation_fn=None)
        return logits


class ESIMv1(BaseESIM):
    def __init__(self, representation_size=300, dropout_keep_prob=1.0, *args, **kwargs):
        self.representation_size = representation_size
        self.dropout_keep_prob = dropout_keep_prob
        super().__init__(*args, **kwargs)

    def _transform_input(self, sequence, sequence_length, reuse=False):
        with tf.variable_scope('transform_input', reuse=reuse) as _:
            cell_fw = tf.contrib.rnn.LSTMCell(self.representation_size, state_is_tuple=True, reuse=reuse,
                                              initializer=tf.contrib.layers.xavier_initializer())
            cell_bw = tf.contrib.rnn.LSTMCell(self.representation_size, state_is_tuple=True, reuse=reuse,
                                              initializer=tf.contrib.layers.xavier_initializer())

            outputs, output_states = tf.nn.bidirectional_dynamic_rnn(
                cell_fw=cell_fw, cell_bw=cell_bw,
                inputs=sequence, sequence_length=sequence_length, dtype=tf.float32)
        return tf.concat(outputs, axis=2)

    def _transform_attend(self, sequence, reuse=False):
        return sequence

    def _transform_compare(self, sequence, reuse=False):
        with tf.variable_scope('transform_compare', reuse=reuse) as _:
            cell_fw = tf.contrib.rnn.LSTMCell(self.representation_size, state_is_tuple=True, reuse=reuse,
                                              initializer=tf.contrib.layers.xavier_initializer())
            cell_bw = tf.contrib.rnn.LSTMCell(self.representation_size, state_is_tuple=True, reuse=reuse,
                                              initializer=tf.contrib.layers.xavier_initializer())
            outputs, output_states = tf.nn.bidirectional_dynamic_rnn(
                    cell_fw=cell_fw, cell_bw=cell_bw,
                    inputs=sequence, dtype=tf.float32)
        # return tf.concat(outputs, axis=2)
        return tf.concat(output_states, axis=2)

    def _transform_aggregate(self, v1_v2, reuse=False):
        with tf.variable_scope('transform_aggregate', reuse=reuse) as _:
            projection = tf.nn.dropout(v1_v2, keep_prob=self.dropout_keep_prob)
            projection = tf.contrib.layers.fully_connected(inputs=projection, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, 0.01),
                                                           biases_initializer=tf.zeros_initializer(),
                                                           activation_fn=tf.nn.tanh)
        return projection
