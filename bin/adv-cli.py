#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import logging
import math
import sys
import os

import numpy as np
import tensorflow as tf

from inferbeddings.io import read_triples, save
from inferbeddings.knowledgebase import Fact, KnowledgeBaseParser

from inferbeddings.parse import parse_clause

from inferbeddings.models import base as models
from inferbeddings.models import similarities

from inferbeddings.models.training import losses, pairwise_losses, constraints, corrupt, index
from inferbeddings.models.training.util import make_batches

from inferbeddings.adversarial import Adversarial, GroundLoss

from inferbeddings import evaluation

logger = logging.getLogger(os.path.basename(sys.argv[0]))


def train(session, train_sequences, nb_entities, nb_predicates, nb_batches, seed, similarity_name,
          entity_embedding_size, predicate_embedding_size, hidden_size, unit_cube,
          model_name, loss_name, pairwise_loss_name, margin,
          corrupt_relations, learning_rate, initial_accumulator_value, nb_epochs, parser,
          clauses,
          sar_weight, sar_similarity,
          adv_lr, adversary_epochs, discriminator_epochs, adv_weight, adv_margin,
          adv_batch_size, adv_init_ground, adv_ground_samples, adv_ground_tol,
          adv_pooling, adv_closed_form,
          predicate_l2, predicate_norm, debug, debug_embeddings, all_one_entities):
    index_gen = index.GlorotIndexGenerator()

    # Negative examples (triples) will be generated by replacing either the subject or the object of each
    # training triple with one of such entity indices
    neg_idxs = np.array(sorted(set(parser.entity_to_index.values())))
    # We can eventually also corrupt the relation index of a triple
    neg_rel_idxs = np.array(sorted(set(parser.predicate_to_index.values())))

    subject_corruptor = corrupt.SimpleCorruptor(index_generator=index_gen, candidate_indices=neg_idxs, corrupt_objects=False)
    object_corruptor = corrupt.SimpleCorruptor(index_generator=index_gen, candidate_indices=neg_idxs, corrupt_objects=True)
    relation_corruptor = corrupt.SimpleRelationCorruptor(index_generator=index_gen, candidate_indices=neg_rel_idxs)

    # Saving training examples in two Numpy matrices, Xr (nb_samples, 1) containing predicate ids,
    # and Xe (nb_samples, 2), containing subject and object ids.
    Xr = np.array([[rel_idx] for (rel_idx, _) in train_sequences])
    Xe = np.array([ent_idxs for (_, ent_idxs) in train_sequences])

    nb_samples = Xr.shape[0]

    # Number of samples per batch.
    batch_size = math.ceil(nb_samples / nb_batches)
    logger.info("Samples: %d, no. batches: %d -> batch size: %d" % (nb_samples, nb_batches, batch_size))

    # Input for the subject and object ids.
    entity_inputs = tf.placeholder(tf.int32, shape=[None, 2])

    # Input for the predicate id - at the moment it is a length-1 walk, i.e. a predicate id only,
    # but it can correspond to a sequence of predicates (a walk in the knowledge graph).
    walk_inputs = tf.placeholder(tf.int32, shape=[None, None])

    np.random.seed(seed)
    random_state = np.random.RandomState(seed)
    tf.set_random_seed(seed)

    # Instantiate the model
    similarity_function = similarities.get_function(similarity_name)

    entity_embedding_layer = tf.get_variable('entities', shape=[nb_entities + 1, entity_embedding_size],
                                             initializer=tf.contrib.layers.xavier_initializer())

    predicate_embedding_layer = tf.get_variable('predicates', shape=[nb_predicates + 1, predicate_embedding_size],
                                                initializer=tf.contrib.layers.xavier_initializer())

    entity_embeddings = tf.nn.embedding_lookup(entity_embedding_layer, entity_inputs)
    predicate_embeddings = tf.nn.embedding_lookup(predicate_embedding_layer, walk_inputs)

    model_class = models.get_function(model_name)

    model_parameters = dict(entity_embeddings=entity_embeddings,
                            predicate_embeddings=predicate_embeddings,
                            similarity_function=similarity_function,
                            hidden_size=hidden_size)
    model = model_class(**model_parameters)

    # Scoring function used for scoring arbitrary triples.
    score = model()

    def scoring_function(args):
        return session.run(score, feed_dict={walk_inputs: args[0], entity_inputs: args[1]})

    loss_function = 0.0

    if sar_weight is not None:
        from inferbeddings.regularizers import clauses_to_equality_loss
        sar_loss = clauses_to_equality_loss(model_name=model_name, clauses=clauses,
                                            similarity_name=sar_similarity,
                                            predicate_embedding_layer=predicate_embedding_layer,
                                            predicate_to_index=parser.predicate_to_index)
        loss_function += sar_weight * sar_loss

    adversarial, ground_loss, clause_to_feed_dicts = None, None, None
    initialize_violators, adversarial_optimizer_variables_initializer = None, None

    # Note - you can either use adversarial training using Gradient Ascent, by setting adv_lr,
    # or use closed-form solutions for the adversarial loss, by setting adv_closed_form, but not both.
    assert not (adv_closed_form and (adv_lr is not None))

    if adv_lr is not None:
        adversarial = Adversarial(clauses=clauses, parser=parser,
                                  entity_embedding_layer=entity_embedding_layer,
                                  predicate_embedding_layer=predicate_embedding_layer,
                                  model_class=model_class, model_parameters=model_parameters, loss_margin=adv_margin,
                                  pooling=adv_pooling, batch_size=adv_batch_size)

        if adv_ground_samples is not None:
            ground_loss = GroundLoss(clauses=clauses, parser=parser, scoring_function=scoring_function,
                                     tolerance=adv_ground_tol)

            # For each clause, sample a list of 1024 {variable: entity} mappings
            entity_indices = sorted({idx for idx in parser.entity_to_index.values()})
            clause_to_feed_dicts = {clause: GroundLoss.sample_mappings(GroundLoss.get_variable_names(clause), entities=entity_indices,
                                                                       sample_size=adv_ground_samples) for clause in clauses}

        initialize_violators = tf.variables_initializer(var_list=adversarial.parameters, name='init_violators')
        violation_errors, violation_loss = adversarial.errors, adversarial.loss

        adv_opt_scope_name = 'adversarial/optimizer'
        with tf.variable_scope(adv_opt_scope_name):
            violation_finding_optimizer = tf.train.AdagradOptimizer(learning_rate=adv_lr, initial_accumulator_value=initial_accumulator_value)
            violation_training_step = violation_finding_optimizer.minimize(- violation_loss, var_list=adversarial.parameters)

        adversarial_optimizer_variables = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope=adv_opt_scope_name)
        adversarial_optimizer_variables_initializer = tf.variables_initializer(adversarial_optimizer_variables)

        loss_function += adv_weight * violation_loss

        adv_entity_projections = [constraints.unit_sphere(adv_embedding_layer, norm=1.0) for adv_embedding_layer in adversarial.parameters]
        if unit_cube:
            adv_entity_projections = [constraints.unit_cube(adv_embedding_layer) for adv_embedding_layer in adversarial.parameters]

        adversarial_projection_steps = adv_entity_projections

    if adv_closed_form:
        from inferbeddings.adversarial.closedform import ClosedFormLifted
        closed_form_lifted = ClosedFormLifted(parser=parser,
                                              predicate_embedding_layer=predicate_embedding_layer,
                                              model_class=model_class, model_parameters=model_parameters,
                                              is_unit_cube=True)
        for clause in clauses:
            clause_violation_loss = closed_form_lifted(clause)
            loss_function += adv_weight * clause_violation_loss

    # For each training triple, we have three versions: one (positive) triple and two (negative) triples,
    # obtained by corrupting the original training triple.
    nb_versions = 3

    if corrupt_relations:
        # If we also corrupt relations, we have 4 versions of each training triple:
        # - the original (positive) triple
        # - two (negative) triples obtained by corrupting first the subject and then the object
        # - one (negative) triple obtained by corrupting the relation
        nb_versions = 4

    # Loss function to minimize by means of Stochastic Gradient Descent.
    fact_loss = 0.0
    if loss_name is not None:
        # We are now using a classic (scores, targets) loss from models/training/losses.py
        loss = losses.get_function(loss_name)

        # Generate a vector of targets - given that each positive example is followed by
        # two negative examples, create a targets vector like [1, 0, 0, 1, 0, 0, 1, 0, 0 ..]
        # > tf.cast((tf.range(0, limit=12) % 3) < 1, dtype=tf.int32).eval()
        # array([1, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0], dtype=int32)

        target = ((tf.range(0, limit=tf.shape(score)[0]) % nb_versions) < 1)
        fact_loss += loss(score, tf.cast(target, score.dtype), margin=margin)
    else:
        # We are now using a pairwise (positives, negatives) loss from models/training/pairwise_losses.py

        # Transform the pairwise loss function in an unary loss function,
        # where each positive example is followed by two negative examples.
        def loss_modifier(_loss_function):
            def unary_function(_score, *_args, **_kwargs):
                if corrupt_relations:
                    # if corrupt_relations is true, then nb_versions = 4.
                    assert nb_versions == 4
                    # tf.reshape(x, [-1, 4]) turns an [M]-dimensional score vector into a [M/4, 4] dimensional one
                    # tf.split(1, 4, x) turns a [N, 4]-dimensional score matrix into four [N]-dimensional ones
                    positive_scores, neg_left, neg_central, neg_right = tf.split(axis=1, num_or_size_splits=nb_versions,
                                                                                 value=tf.reshape(_score, [-1, nb_versions]))
                    _loss_left = _loss_function(positive_scores, neg_left, *_args, **_kwargs)
                    _loss_central = _loss_function(positive_scores, neg_central, *_args, **_kwargs)
                    _loss_right = _loss_function(positive_scores, neg_right, *_args, **_kwargs)
                    _loss = _loss_left + _loss_central + _loss_right
                else:
                    assert nb_versions == 3
                    # tf.reshape(x, [-1, 3]) turns an [M]-dimensional score vector into a [M/3, 3] dimensional one
                    # tf.split(1, 3, x) turns a [N, 3]-dimensional score matrix into three [N]-dimensional ones
                    positive_scores, negative_scores_left, negative_scores_right = tf.split(axis=1, num_or_size_splits=nb_versions,
                                                                                            value=tf.reshape(_score, [-1, nb_versions]))
                    _loss_left = _loss_function(positive_scores, negative_scores_left, *_args, **_kwargs)
                    _loss_right = _loss_function(positive_scores, negative_scores_right, *_args, **_kwargs)
                    _loss = _loss_left + _loss_right
                return _loss
            return unary_function

        pairwise_loss = loss_modifier(pairwise_losses.get_function(pairwise_loss_name))
        fact_loss += pairwise_loss(score, margin=margin)

    if predicate_l2 is not None:
        fact_loss += tf.nn.l2_loss(predicate_embedding_layer)

    loss_function += fact_loss

    # Optimization algorithm being used.
    optimizer = tf.train.AdagradOptimizer(learning_rate=learning_rate,
                                          initial_accumulator_value=initial_accumulator_value)
    trainable_var_list = [entity_embedding_layer, predicate_embedding_layer] + model.get_params()
    training_step = optimizer.minimize(loss_function, var_list=trainable_var_list)

    # We enforce all entity embeddings to have an unitary norm, or to live in the unit cube.
    entity_projection = constraints.unit_sphere(entity_embedding_layer, norm=1.0)
    if unit_cube:
        entity_projection = constraints.unit_cube(entity_embedding_layer)
    projection_steps = [entity_projection]
    if predicate_norm is not None:
        projection_steps += [constraints.renorm_update(predicate_embedding_layer, norm=predicate_norm)]

    if all_one_entities is not None:
        for all_one_entity in all_one_entities:
            # Make sure all entities which have to be associated to all-ones embeddings actually exist
            assert all_one_entity in parser.entity_to_index

            all_one_entity_idx = parser.entity_to_index[all_one_entity]
            _ones = tf.ones_like(entity_embedding_layer[all_one_entity_idx, :])
            projection_steps += [entity_embedding_layer[all_one_entity_idx, :].assign(_ones)]

    init_op = tf.global_variables_initializer()
    session.run(init_op)

    prev_embedding_matrix = None

    for epoch in range(1, nb_epochs + 1):

        # This is a {clause:list[dict]} dictionary that maps each clause to a list[feed_dict], where each feed_dict
        # provides a {variable:entity}
        if clause_to_feed_dicts is not None:
            sum_errors = 0
            for clause_idx, clause in enumerate(clauses):
                nb_errors = ground_loss.zero_one_errors(clause=clause, feed_dicts=clause_to_feed_dicts[clause])
                logger.info('Epoch: {}\tClause index: {}\tZero-One Errors: {}'.format(epoch, clause_idx, nb_errors))
                sum_errors += nb_errors
            logger.info('Epoch: {}\tSum of Zero-One Errors: {}'.format(epoch, sum_errors))

        for disc_epoch in range(1, discriminator_epochs + 1):
            order = random_state.permutation(nb_samples)
            Xr_shuf, Xe_shuf = Xr[order, :], Xe[order, :]

            Xr_sc, Xe_sc = subject_corruptor(Xr_shuf, Xe_shuf)
            Xr_oc, Xe_oc = object_corruptor(Xr_shuf, Xe_shuf)

            if corrupt_relations:
                Xr_rc, Xe_rc = relation_corruptor(Xr_shuf, Xe_shuf)

            batches = make_batches(nb_samples, batch_size)

            loss_values, violation_loss_values = [], []
            total_fact_loss_value = 0

            for batch_start, batch_end in batches:
                curr_batch_size = batch_end - batch_start

                Xr_batch = np.zeros((curr_batch_size * nb_versions, Xr_shuf.shape[1]), dtype=Xr_shuf.dtype)
                Xe_batch = np.zeros((curr_batch_size * nb_versions, Xe_shuf.shape[1]), dtype=Xe_shuf.dtype)

                Xr_batch[0::nb_versions, :] = Xr_shuf[batch_start:batch_end, :]
                Xe_batch[0::nb_versions, :] = Xe_shuf[batch_start:batch_end, :]

                Xr_batch[1::nb_versions, :], Xe_batch[1::nb_versions, :] = Xr_sc[batch_start:batch_end, :], Xe_sc[batch_start:batch_end, :]
                Xr_batch[2::nb_versions, :], Xe_batch[2::nb_versions, :] = Xr_oc[batch_start:batch_end, :], Xe_oc[batch_start:batch_end, :]

                if corrupt_relations:
                    Xr_batch[3::nb_versions, :], Xe_batch[3::nb_versions, :] = Xr_rc[batch_start:batch_end, :], Xe_rc[batch_start:batch_end, :]

                # Safety check - each positive example is followed by two negative (corrupted) examples
                assert Xr_batch[0] == Xr_batch[1] == Xr_batch[2]
                assert Xe_batch[0, 0] == Xe_batch[2, 0] and Xe_batch[0, 1] == Xe_batch[1, 1]
                if corrupt_relations:
                    assert Xr_batch[0] == Xr_batch[1] == Xr_batch[2]
                    assert Xe_batch[0, 0] == Xe_batch[2, 0] == Xe_batch[3, 0]
                    assert Xe_batch[0, 1] == Xe_batch[1, 1] == Xe_batch[3, 1]

                loss_args = {walk_inputs: Xr_batch, entity_inputs: Xe_batch}

                # Update Parameters and Compute Loss
                if adv_lr is not None:
                    _, loss_value, fact_loss_value, violation_loss_value = session.run(
                        [training_step, loss_function, fact_loss, violation_loss], feed_dict=loss_args)
                    violation_loss_values += [violation_loss_value]
                else:
                    _, loss_value, fact_loss_value = session.run([training_step, loss_function, fact_loss],
                                                                 feed_dict=loss_args)

                loss_values += [loss_value / (Xr_batch.shape[0] / nb_versions)]
                total_fact_loss_value += fact_loss_value

                # Project parameters
                for projection_step in projection_steps:
                    session.run([projection_step])

            def stats(values):
                return '{0:.4f} ± {1:.4f}'.format(round(np.mean(values), 4), round(np.std(values), 4))

            logger.info('Epoch: {0}/{1}\tLoss: {2}'.format(epoch, disc_epoch, stats(loss_values)))
            logger.info('Epoch: {0}/{1}\tFact Loss: {2:.4f}'.format(epoch, disc_epoch, total_fact_loss_value))

            if adv_lr is not None:
                logger.info(
                    'Epoch: {0}/{1}\tViolation Loss: {2}'.format(epoch, disc_epoch, stats(violation_loss_values)))

            if debug_embeddings is not None:
                # Saving the parameters of the discriminator (entity and predicate embeddings)
                objects_to_serialize = {
                    'entity_to_index': parser.entity_to_index,
                    'predicate_to_index': parser.predicate_to_index,
                    'entities': entity_embedding_layer.eval(),
                    'predicates': predicate_embedding_layer.eval()
                }
                save('{}_discriminator_{}.pkl'.format(debug_embeddings, epoch), objects_to_serialize)

            # Check if the fact loss is NaN, and stop if it happens
            if np.isnan(total_fact_loss_value):
                logger.error('Epoch: {0}/{1}\tFact loss is NaN! Exiting ..'.format(epoch, disc_epoch))
                sys.exit(0)

        if adv_lr is not None:
            logger.info('Finding violators ..')

            session.run([initialize_violators, adversarial_optimizer_variables_initializer])

            if adv_init_ground:
                # Initialize the violating embeddings using real embeddings
                def ground_init_op(violating_embeddings):
                    # Select adv_batch_size random entity indices - first collect all entity indices
                    _ent_indices = np.array(sorted(parser.index_to_entity.keys()))
                    # Then select a subset of size adv_batch_size of such indices
                    rnd_ent_indices = _ent_indices[
                        random_state.randint(low=0, high=len(_ent_indices), size=adv_batch_size)]
                    # Assign the embeddings of the entities at such indices to the violating embeddings
                    _ent_embeddings = tf.nn.embedding_lookup(entity_embedding_layer, rnd_ent_indices)
                    return violating_embeddings.assign(_ent_embeddings)

                assignment_ops = [ground_init_op(violating_emb) for violating_emb in adversarial.parameters]
                session.run(assignment_ops)

            for projection_step in adversarial_projection_steps:
                session.run([projection_step])

            for finding_epoch in range(1, adversary_epochs + 1):
                _, violation_errors_value, violation_loss_value = session.run(
                    [violation_training_step, violation_errors, violation_loss])

                if finding_epoch == 1 or finding_epoch % 10 == 0:
                    logger.info('Epoch: {}, Finding Epoch: {}, Violated Clauses: {}, Violation loss: {}'
                                .format(epoch, finding_epoch, int(violation_errors_value),
                                        round(violation_loss_value, 4)))

                for projection_step in adversarial_projection_steps:
                    session.run([projection_step])

            if debug_embeddings is not None:
                # Saving the parameters of the generator/adversary (entity and predicate embeddings)
                objects_to_serialize = {
                    'entity_to_index': parser.entity_to_index,
                    'predicate_to_index': parser.predicate_to_index,
                    'variables': {variable.name: variable.eval() for variable in adversarial.parameters},
                    'predicates': predicate_embedding_layer.eval()
                }
                save('{}_adversary_{}.pkl'.format(debug_embeddings, epoch), objects_to_serialize)

        if debug:
            from inferbeddings.visualization import hinton_diagram

            embedding_matrix = session.run(predicate_embedding_layer)[1:, :]
            print(hinton_diagram(embedding_matrix))

            if prev_embedding_matrix is not None:
                diff = prev_embedding_matrix - embedding_matrix
                diff_norm, norm = np.abs(diff).sum(), np.abs(embedding_matrix).sum()
                logger.info('Epoch: {}, Update to Predicate Embeddings: {} Norm: {}'.format(epoch, diff_norm, norm))

            # Clause weights, being explored by @riedelcastro
            # for clause, weight in adversarial.weights.items():
            #     print("{clause} < {weight} >".format(clause=clause, weight=session.run(weight)))

            prev_embedding_matrix = embedding_matrix

    objects = {
        'entity_embedding_layer': entity_embedding_layer,
        'predicate_embedding_layer': predicate_embedding_layer
    }

    return scoring_function, objects


def main(argv):
    logger.info('Command line: {}'.format(' '.join(arg for arg in argv)))

    def formatter(prog):
        return argparse.HelpFormatter(prog, max_help_position=100, width=200)

    argparser = argparse.ArgumentParser('Rule Injection via Adversarial Training', formatter_class=formatter)

    argparser.add_argument('--train', '-t', required=True, action='store', type=str, default=None)
    argparser.add_argument('--valid', '-v', action='store', type=str, default=None)
    argparser.add_argument('--test', '-T', action='store', type=str, default=None)

    argparser.add_argument('--debug', '-D', action='store_true', help='Debug flag')
    argparser.add_argument('--debug-scores', nargs='+', type=str,
                           help='List of files containing triples we want to compute the score of')
    argparser.add_argument('--debug-embeddings', action='store', type=str, default=None)
    argparser.add_argument('--debug-results', action='store_true', help='Report fine-grained ranking results')

    argparser.add_argument('--lr', '-l', action='store', type=float, default=0.1)
    argparser.add_argument('--initial-accumulator-value', action='store', type=float, default=0.1)

    argparser.add_argument('--nb-batches', '-b', action='store', type=int, default=10)
    argparser.add_argument('--nb-epochs', '-e', action='store', type=int, default=100)

    argparser.add_argument('--model', '-m', action='store', type=str, default='DistMult', help='Model')
    argparser.add_argument('--similarity', '-s', action='store', type=str, default='dot', help='Similarity function')

    argparser.add_argument('--loss', action='store', type=str, default=None, help='Loss function')
    argparser.add_argument('--pairwise-loss', action='store', type=str, default='hinge_loss',
                           help='Pairwise loss function')
    argparser.add_argument('--corrupt-relations', action='store_true',
                           help='Also corrupt the relation of each training triple for generating negative examples')

    argparser.add_argument('--margin', '-M', action='store', type=float, default=1.0, help='Margin')

    argparser.add_argument('--embedding-size', '--entity-embedding-size', '-k', action='store', type=int, default=10,
                           help='Entity embedding size')
    argparser.add_argument('--predicate-embedding-size', '-p', action='store', type=int, default=None,
                           help='Predicate embedding size')
    argparser.add_argument('--hidden-size', '-H', action='store', type=int, default=None,
                           help='Size of the hidden layer (if necessary, e.g. ER-MLP)')
    argparser.add_argument('--unit-cube', action='store_true',
                           help='Project all entity embeddings on the unit cube (rather than the unit sphere)')

    argparser.add_argument('--all-one-entities', nargs='+', type=str,
                           help='Entities with all-one entity embeddings')

    argparser.add_argument('--predicate-l2', action='store', type=float, default=None,
                           help='Weight of the L2 regularization term on the predicate embeddings')
    argparser.add_argument('--predicate-norm', action='store', type=float, default=None,
                           help='Norm of the predicate embeddings')

    argparser.add_argument('--auc', '-a', action='store_true',
                           help='Measure the predictive accuracy using AUC-PR and AUC-ROC')
    argparser.add_argument('--seed', '-S', action='store', type=int, default=0, help='Seed for the PRNG')

    argparser.add_argument('--clauses', '-c', action='store', type=str, default=None,
                           help='File containing background knowledge expressed as Horn clauses')

    argparser.add_argument('--sar-weight', action='store', type=float, default=None,
                           help='Schema-Aware Regularization, regularizer weight')
    argparser.add_argument('--sar-similarity', action='store', type=str, default='l2_sqr',
                           help='Schema-Aware Regularization, similarity measure')

    argparser.add_argument('--adv-lr', '-L', action='store', type=float, default=None, help='Adversary learning rate')

    argparser.add_argument('--adversary-epochs', action='store', type=int, default=10,
                           help='Adversary - number of training epochs')
    argparser.add_argument('--discriminator-epochs', action='store', type=int, default=1,
                           help='Discriminator - number of training epochs')

    argparser.add_argument('--adv-weight', '-W', action='store', type=float, default=1.0, help='Adversary weight')
    argparser.add_argument('--adv-margin', action='store', type=float, default=0.0, help='Adversary margin')

    argparser.add_argument('--adv-batch-size', action='store', type=int, default=1,
                           help='Size of the batch of adversarial examples to use')
    argparser.add_argument('--adv-init-ground', action='store_true',
                           help='Initialize adversarial embeddings using real entity embeddings')

    argparser.add_argument('--adv-ground-samples', action='store', type=int, default=None,
                           help='Number of ground samples on which to compute the ground loss')
    argparser.add_argument('--adv-ground-tol', '--adv-ground-tolerance', action='store', type=float, default=0.0,
                           help='Epsilon-tolerance when calculating the ground loss')

    argparser.add_argument('--adv-pooling', action='store', type=str, default='sum',
                           help='Pooling method used for aggregating adversarial losses (sum, mean, max, logsumexp)')

    argparser.add_argument('--adv-closed-form', action='store_true',
                           help='Whenever possible, use closed form solutions for training the adversary')

    argparser.add_argument('--subsample-size', action='store', type=float, default=None,
                           help='Fraction of training facts to use during training (e.g. 0.1)')
    argparser.add_argument('--head-subsample-size', action='store', type=float, default=None,
                           help='Fraction of training facts to use during training (e.g. 0.1)')

    argparser.add_argument('--materialize', action='store_true',
                           help='Materialize all facts using clauses and logical inference')
    argparser.add_argument('--save', action='store', type=str, default=None,
                           help='Path for saving the serialized model')

    args = argparser.parse_args(argv)

    train_path, valid_path, test_path = args.train, args.valid, args.test
    nb_batches, nb_epochs = args.nb_batches, args.nb_epochs
    learning_rate, initial_accumulator_value, margin = args.lr, args.initial_accumulator_value, args.margin

    model_name, similarity_name = args.model, args.similarity
    loss_name, pairwise_loss_name = args.loss, args.pairwise_loss
    corrupt_relations = args.corrupt_relations
    entity_embedding_size, predicate_embedding_size = args.embedding_size, args.predicate_embedding_size
    hidden_size = args.hidden_size
    unit_cube = args.unit_cube

    all_one_entities = args.all_one_entities

    predicate_l2, predicate_norm = args.predicate_l2, args.predicate_norm

    if predicate_embedding_size is None:
        predicate_embedding_size = entity_embedding_size

    is_auc = args.auc
    seed = args.seed
    debug = args.debug
    debug_embeddings = args.debug_embeddings

    clauses_path = args.clauses

    sar_weight, sar_similarity = args.sar_weight, args.similarity

    adv_lr, adv_weight, adv_margin = args.adv_lr, args.adv_weight, args.adv_margin
    adversary_epochs, discriminator_epochs = args.adversary_epochs, args.discriminator_epochs
    adv_ground_samples, adv_ground_tol = args.adv_ground_samples, args.adv_ground_tol
    adv_batch_size, adv_init_ground = args.adv_batch_size, args.adv_init_ground
    adv_pooling = args.adv_pooling
    adv_closed_form = args.adv_closed_form

    subsample_size = args.subsample_size
    head_subsample_size = args.head_subsample_size

    save_path = args.save
    is_materialize = args.materialize

    assert train_path is not None
    pos_train_triples, _ = read_triples(train_path)
    pos_valid_triples, neg_valid_triples = read_triples(valid_path) if valid_path else (None, None)
    pos_test_triples, neg_test_triples = read_triples(test_path) if test_path else (None, None)

    def fact(s, p, o):
        return Fact(predicate_name=p, argument_names=[s, o])

    train_facts = [fact(s, p, o) for s, p, o in pos_train_triples]

    valid_facts = [fact(s, p, o) for s, p, o in pos_valid_triples] if pos_valid_triples is not None else []
    valid_facts_neg = [fact(s, p, o) for s, p, o in neg_valid_triples] if neg_valid_triples is not None else []

    test_facts = [fact(s, p, o) for s, p, o in pos_test_triples] if pos_test_triples is not None else []
    test_facts_neg = [fact(s, p, o) for s, p, o in neg_test_triples] if neg_test_triples is not None else []

    logger.info('#Training: {}, #Validation: {}, #Test: {}'
                .format(len(train_facts), len(valid_facts), len(test_facts)))

    parser = KnowledgeBaseParser(train_facts + valid_facts + test_facts)

    nb_entities = len(parser.entity_vocabulary)
    nb_predicates = len(parser.predicate_vocabulary)

    # Entity and predicate indices start at 1 (index 0 can be used for a missing entity or predicate)
    assert (0 not in set(parser.entity_to_index.values()))
    assert (0 not in set(parser.predicate_to_index.values()))

    logger.info('#Entities: {}\t#Predicates: {}'.format(nb_entities, nb_predicates))

    # Subsampling training facts for X-shot learning
    if subsample_size is not None and subsample_size < 1:
        assert subsample_size >= .0
        nb_train_facts = len(train_facts)
        sample_size = int(round(nb_train_facts * subsample_size))

        logger.info('Randomly selecting {} triples from the training set'.format(sample_size))
        random_state = np.random.RandomState(seed=seed)
        sample_indices = random_state.choice(nb_train_facts, sample_size, replace=False)

        _train_facts = [train_facts[idx] for idx in sample_indices]
        train_facts = _train_facts

    # Parse the clauses
    clauses = None
    if clauses_path is not None:
        with open(clauses_path, 'r') as f:
            clauses = [parse_clause(line.strip()) for line in f.readlines()]

    # Subsampling training facts that appear in the clause heads for X-shot learning
    if head_subsample_size is not None and head_subsample_size < 1:
        assert head_subsample_size >= .0
        assert clauses is not None

        # Listing all predicate indexes
        predicate_idxs = sorted({parser.predicate_to_index[train_fact.predicate_name] for train_fact in train_facts})

        # Listing the predicate indexes used in clause heads:
        predicate_idxs_in_clause_heads = sorted({parser.predicate_to_index[c.head.predicate.name] for c in clauses})

        _train_facts = []

        # Iterate over all predicate indexes
        for predicate_idx in predicate_idxs:
            # Select all facts with predicate predicate_idx
            predicate_facts = [f for f in train_facts if parser.predicate_to_index[f.predicate_name] == predicate_idx]

            # If predicate_idx appears in the head of a clause, subsample it
            if predicate_idx in predicate_idxs_in_clause_heads:
                nb_predicate_facts = len(predicate_facts)
                sample_size = int(round(nb_predicate_facts * head_subsample_size))

                logger.info('Randomly selecting {} triples for predicate {} from the training set'
                            .format(sample_size, predicate_idx))

                random_state = np.random.RandomState(seed=seed)
                sample_indices = random_state.choice(nb_predicate_facts, sample_size, replace=False)

                _predicate_facts = [predicate_facts[idx] for idx in sample_indices]
                _train_facts += _predicate_facts
            # Otherwise do nothing
            else:
                _train_facts += predicate_facts

        train_facts = _train_facts

    if is_materialize:
        logger.info('Materializing the Knowledge Base using Logical Inference')
        assert clauses is not None

        nb_train_facts = len(train_facts)
        logger.info('Number of starting facts: {}'.format(nb_train_facts))

        from inferbeddings.logic import materialize
        inferred_train_facts = materialize(train_facts, clauses, parser)
        nb_inferred_facts = len(inferred_train_facts)
        logger.info('Number of (new) inferred facts: {}'.format(nb_inferred_facts - nb_train_facts))

        # We should have an equal or higher number of facts now
        assert nb_inferred_facts >= nb_train_facts

        train_facts = inferred_train_facts

    train_sequences = parser.facts_to_sequences(train_facts)

    valid_sequences = parser.facts_to_sequences(valid_facts)
    valid_sequences_neg = parser.facts_to_sequences(valid_facts_neg)

    test_sequences = parser.facts_to_sequences(test_facts)
    test_sequences_neg = parser.facts_to_sequences(test_facts_neg)

    if adv_lr is not None:
        assert clauses_path is not None

    # Do not take up all the GPU memory, all the time.
    sess_config = tf.ConfigProto()
    sess_config.gpu_options.allow_growth = True

    with tf.Session(config=sess_config) as session:
        scoring_function, objects = train(session, train_sequences, nb_entities, nb_predicates, nb_batches, seed,
                                          similarity_name,
                                          entity_embedding_size, predicate_embedding_size, hidden_size, unit_cube,
                                          model_name, loss_name, pairwise_loss_name, margin,
                                          corrupt_relations, learning_rate, initial_accumulator_value, nb_epochs, parser,
                                          clauses,
                                          sar_weight, sar_similarity,
                                          adv_lr, adversary_epochs, discriminator_epochs, adv_weight, adv_margin,
                                          adv_batch_size, adv_init_ground, adv_ground_samples, adv_ground_tol,
                                          adv_pooling, adv_closed_form,
                                          predicate_l2, predicate_norm, debug, debug_embeddings, all_one_entities)

        if args.debug_scores is not None:
            # Print the scores of all triples contained in args.debug_scores
            for path in args.debug_scores:
                debug_triples, _ = read_triples(path)
                debug_sequences = parser.facts_to_sequences([fact(s, p, o) for s, p, o in debug_triples])
                for debug_triple, (p, [s, o]) in zip(debug_triples, debug_sequences):
                    debug_score = scoring_function([[[p]], [[s, o]]])[0]
                    print('{}\tTriple: {}\tScore: {}'.format(path, debug_triple, debug_score))
                    debug_score_inverse = scoring_function([[[p]], [[o, s]]])[0]
                    print('{}\tInverse Triple: {}\tScore: {}'.format(path, debug_triple, debug_score_inverse))

        if save_path is not None:
            objects_to_serialize = {
                'command_line': argv,
                'entity_to_index': parser.entity_to_index,
                'predicate_to_index': parser.predicate_to_index,
                'entities': objects['entity_embedding_layer'].eval(),
                'predicates': objects['predicate_embedding_layer'].eval()
            }

            save(save_path, objects_to_serialize)

            saver = tf.train.Saver()
            save_path = saver.save(session, '{}.model.ckpt'.format(save_path))
            logger.info('Model saved in {}'.format(save_path))

        train_triples = [(s, p, o) for (p, [s, o]) in train_sequences]

        valid_triples = [(s, p, o) for (p, [s, o]) in valid_sequences]
        valid_triples_neg = [(s, p, o) for (p, [s, o]) in valid_sequences_neg]

        test_triples = [(s, p, o) for (p, [s, o]) in test_sequences]
        test_triples_neg = [(s, p, o) for (p, [s, o]) in test_sequences_neg]

        true_triples = train_triples + valid_triples + test_triples

        if valid_triples:
            if is_auc:
                evaluation.evaluate_auc(scoring_function, valid_triples, valid_triples_neg,
                                        nb_entities, nb_predicates, tag='valid')
            else:
                evaluation.evaluate_ranks(scoring_function, valid_triples,
                                          nb_entities, true_triples=true_triples, tag='valid',
                                          verbose=args.debug_results, index_to_predicate=parser.index_to_predicate)

        if test_triples:
            if is_auc:
                evaluation.evaluate_auc(scoring_function, test_triples, test_triples_neg,
                                        nb_entities, nb_predicates, tag='test')
            else:
                evaluation.evaluate_ranks(scoring_function, test_triples,
                                          nb_entities, true_triples=true_triples, tag='test',
                                          verbose=args.debug_results, index_to_predicate=parser.index_to_predicate)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main(sys.argv[1:])
