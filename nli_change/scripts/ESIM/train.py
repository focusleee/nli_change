import numpy
import os

from main import train

if __name__ == '__main__':
    model_name = os.path.basename(os.path.dirname(os.path.realpath(__file__)))
    train(
    saveto           = './{}.npz'.format(model_name),
    reload_          = False,
    dim_word         = 300,
    dim              = 300,
    patience         = 7,
    n_words          = 42394,
    decay_c          = 0.,
    clip_c           = 10.,
    lrate            = 0.0004,
    optimizer        = 'adam',
    maxlen           = 100,
    batch_size       = 32,
    valid_batch_size = 32,
    dispFreq         = 100,
    validFreq        = int(549367/32+1),
    saveFreq         = int(549367/32+1),
    use_dropout      = True,
    verbose          = False,
    datasets         = ['../../nli_test/word_sequence/premise_snli_1.0_train.txt',
                        '../../nli_test/word_sequence/hypothesis_snli_1.0_train.txt',
                        '../../nli_test/word_sequence/label_snli_1.0_train.txt'],
    valid_datasets   = ['../../nli_test/word_sequence/premise_snli_1.0_dev.txt',
                        '../../nli_test/word_sequence/hypothesis_snli_1.0_dev.txt',
                        '../../nli_test/word_sequence/label_snli_1.0_dev.txt'],
    test_datasets    = ['../../nli_test/word_sequence/premise_snli_1.0_test.txt',
                        '../../nli_test/word_sequence/hypothesis_snli_1.0_test.txt',
                        '../../nli_test/word_sequence/label_snli_1.0_test.txt'],
    dictionary       = '../../nli_test/word_sequence/vocab_cased.pkl',
    embedding        = '../../nli_test/glove/glove.840B.300d.txt',
    matching_rounds   = 3,
    )
