# -*- coding: utf-8 -*-

import numpy as np
import inferbeddings.rte.util as util
import logging

import pytest

logger = logging.getLogger(__name__)


def test_rte_util():
    train_instances, dev_instances, test_instances = util.SNLI.generate()
    all_instances = train_instances + dev_instances + test_instances
    qs_tokenizer, a_tokenizer = util.train_tokenizer_on_instances(all_instances, num_words=None)

    train_dataset = util.to_dataset(train_instances, qs_tokenizer, a_tokenizer)

    train_instances, dev_instances, test_instances = util.SNLI.generate(prefix='<START>')
    all_instances = train_instances + dev_instances + test_instances
    qs_tokenizer, a_tokenizer = util.train_tokenizer_on_instances(all_instances, num_words=None)

    p_train_dataset = util.to_dataset(train_instances, qs_tokenizer, a_tokenizer)

    np.testing.assert_allclose(np.array(train_dataset['question_lengths']) + 1, p_train_dataset['question_lengths'])
    np.testing.assert_allclose(np.array(train_dataset['support_lengths']) + 1, p_train_dataset['support_lengths'])

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    pytest.main([__file__])
