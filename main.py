'''
Build a Enhancing LSTM model for Natural Language Inference
'''
import theano
import theano.tensor as tensor
from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams

import cPickle as pkl
import pdb
import numpy
import copy

import os
import warnings
import sys
import time
import pprint
import logging

from collections import OrderedDict
from data_iterator import TextIterator

profile = False
logger = logging.getLogger(__name__)


# push parameters to Theano shared variables
def zipp(params, tparams):
    for kk, vv in params.iteritems():
        tparams[kk].set_value(vv)


# pull parameters from Theano shared variables
def unzip(zipped):
    new_params = OrderedDict()
    for kk, vv in zipped.iteritems():
        new_params[kk] = vv.get_value()
    return new_params


# get the list of parameters: Note that tparams must be OrderedDict
def itemlist(tparams):
    return [vv for kk, vv in tparams.iteritems()]


# dropout
def dropout_layer(state_before, use_noise, trng):
    """
    tensor switch is like an if statement that checks the
    value of the theano shared variable (use_noise), before
    either dropping out the state_before tensor or
    computing the appropriate activation. During training/testing
    use_noise is toggled on and off.
    """
    proj = tensor.switch(
        use_noise,
        state_before * trng.binomial(state_before.shape, p=0.5, n=1,
                                     dtype=state_before.dtype),
        state_before * 0.5)
    return proj


# make prefix-appended name
def _p(pp, name):
    return '%s_%s' % (pp, name)


# initialize Theano shared variables according to the initial parameters
def init_tparams(params):
    tparams = OrderedDict()
    for kk, pp in params.iteritems():
        tparams[kk] = theano.shared(params[kk], name=kk)
        print kk, pp.shape
    return tparams


# load parameters
def load_params(path, params):
    pp = numpy.load(path)
    for kk, vv in params.iteritems():
        if kk not in pp:
            warnings.warn('%s is not in the archive' % kk)
            continue
        params[kk] = pp[kk]

    return params


"""
Neural network layer definitions.

The life-cycle of each of these layers is as follows
    1) The param_init of the layer is called, which creates
    the weights of the network.
    2) The feedforward is called which builds that part of the Theano graph
    using the weights created in step 1). This automatically links
    these variables to the graph.

Each prefix is used like a key and should be unique
to avoid naming conflicts when building the graph.
"""
# layers: 'name': ('parameter initializer', 'feedforward')
layers = {'ff': ('param_init_fflayer', 'fflayer'),
          'lstm': ('param_init_lstm', 'lstm_layer'),
          'AF_DMN' : ('param_init_AF','AF_DMN_layer'),
          }


def get_layer(name):
    fns = layers[name]
    return (eval(fns[0]), eval(fns[1]))


# some utilities
def ortho_weight(ndim):
    """
    Random orthogonal weights

    Used by norm_weights(below), in which case, we
    are ensuring that the rows are orthogonal
    (i.e W = U \Sigma V, U has the same
    # of rows, V has the same # of cols)
    """
    W = numpy.random.randn(ndim, ndim)
    u, s, v = numpy.linalg.svd(W)
    return u.astype('float32')


def norm_weight(nin, nout=None, scale=0.01, ortho=True):
    """
    Random weights drawn from a Gaussian
    """
    if nout is None:
        nout = nin
    if nout == nin and ortho:
        W = ortho_weight(nin)
    else:
        W = scale * numpy.random.randn(nin, nout)
    return W.astype('float32')


# some useful shorthands
def tanh(x):
    return tensor.tanh(x)


def relu(x):
    return tensor.nnet.relu(x)


def linear(x):
    return x


def concatenate(tensor_list, axis=0):
    """
    Alternative implementation of `theano.tensor.concatenate`.
    This function does exactly the same thing, but contrary to Theano's own
    implementation, the gradient is implemented on the GPU.
    Backpropagating through `theano.tensor.concatenate` yields slowdowns
    because the inverse operation (splitting) needs to be done on the CPU.
    This implementation does not have that problem.
    :usage:
        >>> x, y = theano.tensor.matrices('x', 'y')
        >>> c = concatenate([x, y], axis=1)
    :parameters:
        - tensor_list : list
            list of Theano tensor expressions that should be concatenated.
        - axis : int
            the tensors will be joined along this axis.
    :returns:
        - out : tensor
            the concatenated tensor expression.
    """
    concat_size = sum(tt.shape[axis] for tt in tensor_list)

    output_shape = ()
    for k in range(axis):
        output_shape += (tensor_list[0].shape[k],)
    output_shape += (concat_size,)
    for k in range(axis + 1, tensor_list[0].ndim):
        output_shape += (tensor_list[0].shape[k],)

    out = tensor.zeros(output_shape)
    offset = 0
    for tt in tensor_list:
        indices = ()
        for k in range(axis):
            indices += (slice(None),)
        indices += (slice(offset, offset + tt.shape[axis]),)
        for k in range(axis + 1, tensor_list[0].ndim):
            indices += (slice(None),)

        out = tensor.set_subtensor(out[indices], tt)
        offset += tt.shape[axis]

    return out


# batch preparation
def prepare_data(seqs_x, seqs_y, labels, maxlen=None):
    lengths_x = [len(s) for s in seqs_x]
    lengths_y = [len(s) for s in seqs_y]

    if maxlen is not None:
        new_seqs_x = []
        new_seqs_y = []
        new_lengths_x = []
        new_lengths_y = []
        new_labels = []
        for l_x, s_x, l_y, s_y, l in zip(lengths_x, seqs_x, lengths_y, seqs_y, labels):
            if l_x < maxlen and l_y < maxlen:
                new_seqs_x.append(s_x)
                new_lengths_x.append(l_x)
                new_seqs_y.append(s_y)
                new_lengths_y.append(l_y)
                new_labels.append(l)
        lengths_x = new_lengths_x
        seqs_x = new_seqs_x
        lengths_y = new_lengths_y
        seqs_y = new_seqs_y
        labels = new_labels

        if len(lengths_x) < 1 or len(lengths_y) < 1:
            return None, None, None, None, None

    n_samples = len(seqs_x)
    maxlen_x = numpy.max(lengths_x)
    maxlen_y = numpy.max(lengths_y)

    x = numpy.zeros((maxlen_x, n_samples)).astype('int64')
    y = numpy.zeros((maxlen_y, n_samples)).astype('int64')
    x_mask = numpy.zeros((maxlen_x, n_samples)).astype('float32')
    y_mask = numpy.zeros((maxlen_y, n_samples)).astype('float32')
    l = numpy.zeros((n_samples,)).astype('int64')
    for idx, [s_x, s_y, ll] in enumerate(zip(seqs_x, seqs_y, labels)):
        x[:lengths_x[idx], idx] = s_x
        x_mask[:lengths_x[idx], idx] = 1.
        y[:lengths_y[idx], idx] = s_y
        y_mask[:lengths_y[idx], idx] = 1.
        l[idx] = ll

    return x, x_mask, y, y_mask, l



#todo softmax
def AF_softmax(weight_matrix,x1_mask,x2_mask):
    weight_matrix_1=tensor.exp(weight_matrix - weight_matrix.max(1, keepdims=True)).dimshuffle(1,2,0)
    weight_matrix_2 = tensor.exp(weight_matrix - weight_matrix.max(2, keepdims=True)).dimshuffle(1, 2, 0)
    # weight_matrix_1: #step1 x #step2 x #sample
    weight_matrix_1 = weight_matrix_1 * x1_mask[:,None,:]
    weight_matrix_2 = weight_matrix_2 * x2_mask[None,:,:]
    alpha = weight_matrix_1 / weight_matrix_1.sum(0, keepdims=True)
    beta = weight_matrix_2 / weight_matrix_2.sum(1, keepdims=True)
    return alpha,beta
def _softmax(weight_matrix,mask):
    #weight_matrix_1 [step1,step1,sample# ]
    weight_matrix_1=tensor.exp(weight_matrix - weight_matrix.max(1, keepdims=True)).dimshuffle(1,2,0)
    # weight_matrix_1: #step1 x #step1 x #sample
    weight_matrix_1 = weight_matrix_1 * mask[:, None, :]
    alpha = weight_matrix_1 / weight_matrix_1.sum(0, keepdims=True)
    return alpha
#todo rewrite the matching layer
def param_init_AF(options,params,prefix,nin=None,dim=None):
    if nin is None:
        nin=options['dim']
    if dim is None:
        dim=options['dim']
    W = norm_weight(nin,dim,scale=0.01)
    U_r = norm_weight(nin, 1, scale=0.01)
    U_l = norm_weight(nin, 1, scale=0.01)

#    W = theano.shared(norm_weight(nin,nout,scale=0.01,ortho=ortho))
#    U_r=theano.shared(norm_weight(nin,1,scale=0.01,ortho=ortho))
 #   U_l=theano.shared(norm_weight(nin,1,scale=0.01,ortho=ortho))
    params[_p(prefix,'W')]=W
    params[_p(prefix,'U_r')]=U_r
    params[_p(prefix,'U_l')]=U_l
    ctxdim = options['dim']
    params = get_layer('ff')[0](options, params, prefix=_p(prefix,'cross-attention-relu_p'),
                                nin=ctxdim * 5, nout=options['dim'],
                                ortho=False)
    params = get_layer('ff')[0](options, params, prefix=_p(prefix, 'cross-attention-relu_q'),
                                nin=ctxdim * 5, nout=options['dim'],
                                ortho=False)

    params = get_layer(options['encoder'])[0](options, params,
                                              prefix=_p(prefix,'cross-attention-lstm_p'),
                                              nin=options['dim_word'] * 3,
                                              dim=options['dim'])
    params = get_layer(options['encoder'])[0](options, params,
                                              prefix=_p(prefix,'cross-attention-lstm_q'),
                                              nin=options['dim_word'] * 3,
                                              dim=options['dim'])
    params = get_layer('ff')[0](options, params, prefix=_p(prefix, 'self-attention-relu_p'),
                                nin=ctxdim * 7, nout=options['dim'],
                                ortho=False)
    params = get_layer('ff')[0](options, params, prefix=_p(prefix, 'self-attention-relu_q'),
                                nin=ctxdim * 7, nout=options['dim'],
                                ortho=False)

    params = get_layer(options['encoder'])[0](options, params,
                                              prefix=_p(prefix, 'self-attention-lstm_p'),
                                              nin=options['dim_word'] * 5,
                                              dim=options['dim'])
    params = get_layer(options['encoder'])[0](options, params,
                                              prefix=_p(prefix, 'self-attention-lstm_q'),
                                              nin=options['dim_word'] * 5,
                                              dim=options['dim'])
    return params
# todo AF_DMN_layer
def AF_DMN_layer(tparams,options,prefix,sentence1,sentence2,x1_mask,x2_mask):

    trng = RandomStreams(1234)
    use_noise = theano.shared(numpy.float32(0.))
    pre_hp=sentence1
    pre_hq=sentence2
#     #copy
    # to compute A sentens
    #  sent1,2[sentlength,batchsize,dim]
    temp1 = tensor.tensordot(sentence1,tparams[_p(prefix,'W')],[2,0]).dimshuffle(1,0,2)
    # temp2 [sent1,batchsize,sent2]
    temp2 = tensor.batched_dot(temp1,sentence2.dimshuffle(1,2,0)).dimshuffle(1,0,2)
    # temp2 = tensor.batched_dot(temp1.dimshuffle(1,0,2),sentence2.dimshuffle(1,2,0)).dimshuffle(2,0,1)
    #temp3 [sent1,batchsize,sent2]+ [sent1,batchsize,1]
    temp3 = temp2+tensor.addbroadcast(tensor.tensordot(sentence1,tparams[_p(prefix,'U_r')],[2, 0]),2)
    #A [sent2,batchsize,sent1] + [sent2,batchsize,1]
    A = temp3.dimshuffle(2,1,0)+tensor.addbroadcast(tensor.tensordot(sentence2,tparams[_p(prefix,'U_l')],[2, 0]),2)
    # A [batchsize,sent1,sent2]
    A = A.dimshuffle(1,2,0)
    # ap[step1(sum),step2,batchsize] aq[step1,step2(sum),batchsize]
    aq,ap = AF_softmax(A,x1_mask,x2_mask)
    #hp[sent1(sum),batchsize,dim]
    hp = tensor.batched_dot(pre_hq.dimshuffle(1, 2, 0), ap.dimshuffle( 2, 1, 0)).dimshuffle(2,0,1)
    #hq[sent2(sum),batchsize,dim]
    hq = tensor.batched_dot(pre_hp.dimshuffle(1, 2, 0), aq.dimshuffle( 2, 0, 1)).dimshuffle(2,0,1)
    # fusion for cross attention
    inp1 = concatenate([pre_hp, hp, pre_hp - hp, pre_hp * hp], axis=2)
    inq1 = concatenate([pre_hq, hq, pre_hq - hq, pre_hq * hq], axis=2)
    # todo shortcut
    stepp1 = concatenate([inp1,sentence1],axis=2)
    stepq1 = concatenate([inq1,sentence2],axis=2)
    inp1 = get_layer('ff')[1](tparams, stepp1, options, prefix=_p(prefix,'cross-attention-relu_p'), activ='relu')
    inq1 = get_layer('ff')[1](tparams, stepq1, options, prefix=_p(prefix,'cross-attention-relu_q'), activ='relu')
    if options['use_dropout']:
        inp1 = dropout_layer(inp1, use_noise, trng)
        inq1 = dropout_layer(inq1, use_noise, trng)
    #todo shortcut 900
    stepp2 = concatenate([inp1,hp,sentence1],axis=2)
    stepq2 = concatenate([inq1,hq,sentence2],axis=2)

    #fp[step1,batchsize,dim]300
    fp = get_layer(options['decoder'])[1](tparams, stepp2, options,
                                            prefix=_p(prefix,'cross-attention-lstm_p'),
                                            mask=x1_mask)
    #fq[step2,batchsize,dim]
    fq =  get_layer(options['decoder'])[1](tparams, stepq2, options,
                                            prefix=_p(prefix,'cross-attention-lstm_q'),
                                            mask=x2_mask)
     #self-attention
    # sp[sent1,sent1,batchsize] sq[sent2,sent2,batchsize]
    sp = tensor.batched_dot(fp[0].dimshuffle(1, 0, 2), fp[0].dimshuffle( 1, 2, 0)).dimshuffle(1,2,0)
    sq = tensor.batched_dot(fq[0].dimshuffle(1, 0, 2), fq[0].dimshuffle( 1, 2, 0)).dimshuffle(1,2,0)
    # sp[sent1(sum),sent1,batchsize] sq[sent2(sum),sent2,batchsize]
    sp = _softmax(sp.dimshuffle(2,0,1),x1_mask)
    sq = _softmax(sq.dimshuffle(2,0,1),x2_mask)
    # _hp[sent1,batchsize,dim] _hq[sent2,batchsize,dim] sp[sent1(sum),sent1,batchsize] sq[sent2(sum),sent2,batchsize]
    _hp = tensor.batched_dot(fp[0].dimshuffle(1, 2, 0), sp.dimshuffle( 2, 1, 0)).dimshuffle(2,0,1)
    _hq = tensor.batched_dot(fq[0].dimshuffle(1, 2, 0), sq.dimshuffle( 2, 1, 0)).dimshuffle(2,0,1)

    # fusion for self attention
    inp2 = concatenate([fp[0], _hp, fp[0] - _hp, fp[0] * _hp], axis=2)
    inq2 = concatenate([fq[0], _hq, fq[0] - _hq, fq[0] * _hq], axis=2)
    # todo shortcut
    stepp3 = concatenate([inp2,inp1,hp,sentence1],axis=2)
    stepq3 = concatenate([inq2,inq1,hq,sentence2],axis=2)
    inp2 = get_layer('ff')[1](tparams, stepp3, options, prefix=_p(prefix,'self-attention-relu_p'), activ='relu')
    inq2 = get_layer('ff')[1](tparams, stepq3, options, prefix=_p(prefix,'self-attention-relu_q'), activ='relu')
    if options['use_dropout']:
        inp2 = dropout_layer(inp2, use_noise, trng)
        inq2 = dropout_layer(inq2, use_noise, trng)
    #todo shortcut
    stepp4 = concatenate([ inp2, _hp, inp1, hp, sentence1],axis=2)
    stepq4 = concatenate([ inq2, _hq, inq1, hq, sentence2], axis=2)
    final_hp =  get_layer(options['decoder'])[1](tparams, stepp4, options,
                                            prefix=_p(prefix,'self-attention-lstm_p'),
                                            mask=x1_mask)
    final_hq = get_layer(options['decoder'])[1](tparams, stepq4, options,
                                            prefix=_p(prefix,'self-attention-lstm_q'),
                                            mask=x2_mask)

    return final_hp,final_hq



# feedforward layer: affine transformation + point-wise nonlinearity
def param_init_fflayer(options, params, prefix='ff', nin=None, nout=None,
                       ortho=True):
    if nin is None:
        nin = options['dim']
    if nout is None:
        nout = options['dim']
    params[_p(prefix, 'W')] = norm_weight(nin, nout, scale=0.01, ortho=ortho)
    params[_p(prefix, 'b')] = numpy.zeros((nout,)).astype('float32')

    return params


def fflayer(tparams, state_below, options, prefix='rconv',
            activ='lambda x: tensor.tanh(x)', **kwargs):
    return eval(activ)(
        tensor.dot(state_below, tparams[_p(prefix, 'W')]) +
        tparams[_p(prefix, 'b')])


# LSTM layer
def param_init_lstm(options, params, prefix='lstm', nin=None, dim=None):
    if nin is None:
        nin = options['dim']
    if dim is None:
        dim = options['dim']
    """
     Stack the weight matricies for all the gates
     for much cleaner code and slightly faster dot-prods
    """
    # input weights
    W = numpy.concatenate([norm_weight(nin, dim),
                           norm_weight(nin, dim),
                           norm_weight(nin, dim),
                           norm_weight(nin, dim)], axis=1)
    params[_p(prefix, 'W')] = W
    # for the previous hidden activation
    U = numpy.concatenate([ortho_weight(dim),
                           ortho_weight(dim),
                           ortho_weight(dim),
                           ortho_weight(dim)], axis=1)
    params[_p(prefix, 'U')] = U
    params[_p(prefix, 'b')] = numpy.zeros((4 * dim,)).astype('float32')

    return params


# This function implements the lstm fprop
def lstm_layer(tparams, state_below, options, prefix='lstm', mask=None, **kwargs):
    nsteps = state_below.shape[0]
    dim = tparams[_p(prefix, 'U')].shape[0]

    n_samples = state_below.shape[1]
    init_state = tensor.alloc(0., n_samples, dim)
    init_memory = tensor.alloc(0., n_samples, dim)

    # if we have no mask, we assume all the inputs are valid
    if mask == None:
        mask = tensor.alloc(1., state_below.shape[0], 1)

    # use the slice to calculate all the different gates
    def _slice(_x, n, dim):
        if _x.ndim == 3:
            return _x[:, :, n * dim:(n + 1) * dim]
        elif _x.ndim == 2:
            return _x[:, n * dim:(n + 1) * dim]
        return _x[n * dim:(n + 1) * dim]

    # one time step of the lstm
    def _step(m_, x_, h_, c_):
        preact = tensor.dot(h_, tparams[_p(prefix, 'U')])
        preact += x_

        i = tensor.nnet.sigmoid(_slice(preact, 0, dim))
        f = tensor.nnet.sigmoid(_slice(preact, 1, dim))
        o = tensor.nnet.sigmoid(_slice(preact, 2, dim))
        c = tensor.tanh(_slice(preact, 3, dim))

        c = f * c_ + i * c
        c = m_[:, None] * c + (1. - m_)[:, None] * c_

        h = o * tensor.tanh(c)
        h = m_[:, None] * h + (1. - m_)[:, None] * h_

        return h, c, i, f, o, preact

    state_below = tensor.dot(state_below, tparams[_p(prefix, 'W')]) + tparams[_p(prefix, 'b')]

    rval, updates = theano.scan(_step,
                                sequences=[mask, state_below],
                                outputs_info=[init_state, init_memory, None, None, None, None],
                                name=_p(prefix, '_layers'),
                                n_steps=nsteps, profile=False)
    return rval


# initialize all parameters
def init_params(options, worddicts):
    params = OrderedDict()

    # embedding
    params['Wemb'] = norm_weight(options['n_words'], options['dim_word'])
    # read embedding from GloVe
    if options['embedding']:
        with open(options['embedding'], 'r') as f:
            for line in f:
                tmp = line.split()
                word = tmp[0]
                vector = tmp[1:]
                if word in worddicts and worddicts[word] < options['n_words']:
                    params['Wemb'][worddicts[word], :] = vector

    # encoder: bidirectional RNN
    params = get_layer(options['encoder'])[0](options, params,
                                              prefix='encoder',
                                              nin=options['dim_word'],
                                              dim=options['dim'])

    params = get_layer(options['encoder'])[0](options, params,
                                              prefix='encoder_r',
                                              nin=options['dim_word'],
                                              dim=options['dim'])
    for k in range(0,options['matching_rounds'],1):
    # todo AF_DNM
        num='%d'%k
        params = get_layer('AF_DMN')[0](options, params,
                                         prefix=_p('AF_DMN',num),
                                         nin=options['dim_word'],
                                         dim=options['dim_word'])
        params = get_layer('AF_DMN')[0](options, params,
                                         prefix=_p('AF_DMN_r',num),
                                         nin=options['dim_word'],
                                         dim=options['dim_word'])

    ctxdim = options['dim'] * 2

    # classifier
    params = get_layer('ff')[0](options, params, prefix='ff_layer_1',
                                nin=options['dim'] * 8, nout=options['dim'], ortho=False)
    params = get_layer('ff')[0](options, params, prefix='ff_layer_output',
                                nin=options['dim'], nout=3, ortho=False)

    return params


# build a training model
def build_model(tparams, options):
    """ Builds the entire computational graph used for training
    """
    opt_ret = dict()

    trng = RandomStreams(1234)
    use_noise = theano.shared(numpy.float32(0.))

    # description string: #words x #samples
    x1 = tensor.matrix('x1', dtype='int64')
    x1_mask = tensor.matrix('x1_mask', dtype='float32')
    x2 = tensor.matrix('x2', dtype='int64')
    x2_mask = tensor.matrix('x2_mask', dtype='float32')
    y = tensor.vector('y', dtype='int64')

    xr1 = x1[::-1]
    xr1_mask = x1_mask[::-1]
    xr2 = x2[::-1]
    xr2_mask = x2_mask[::-1]

    n_timesteps_x1 = x1.shape[0]
    n_timesteps_x2 = x2.shape[0]
    n_samples = x1.shape[1]

    # word embedding
    emb1 = tparams['Wemb'][x1.flatten()].reshape([n_timesteps_x1, n_samples, options['dim_word']])
    if options['use_dropout']:
        emb1 = dropout_layer(emb1, use_noise, trng)
    embr1 = emb1[::-1]

    emb2 = tparams['Wemb'][x2.flatten()].reshape([n_timesteps_x2, n_samples, options['dim_word']])
    if options['use_dropout']:
        emb2 = dropout_layer(emb2, use_noise, trng)
    embr2 = emb2[::-1]

    # encode premise

    proj1 = get_layer(options['encoder'])[1](tparams, emb1, options,
                                             prefix='encoder',
                                             mask=x1_mask)
    projr1 = get_layer(options['encoder'])[1](tparams, embr1, options,
                                              prefix='encoder_r',
                                              mask=xr1_mask)

    # context will be the concatenation of forward and backward rnns
    ctx1 = concatenate([proj1[0], projr1[0][::-1]], axis=proj1[0].ndim - 1)

    # encode hypothesis
    proj2 = get_layer(options['encoder'])[1](tparams, emb2, options,
                                             prefix='encoder',
                                             mask=x2_mask)
    projr2 = get_layer(options['encoder'])[1](tparams, embr2, options,
                                              prefix='encoder_r',
                                              mask=xr2_mask)

    # context will be the concatenation of forward and backward rnns
    ctx2 = concatenate([proj2[0], projr2[0][::-1]], axis=proj2[0].ndim - 1)
    #

    # TODO proj3 projr3 proj4 projr4
    proj1[0] = proj1[0] * x1_mask[:, :, None]
    proj2[0] = proj2[0] * x2_mask[:, :, None]
    projr1[0] = projr1[0] * xr1_mask[:, :, None]
    projr2[0] = projr2[0] * xr2_mask[:, :, None]
    for k in range(0, options['matching_rounds'], 1):
         num='%d' %k
         proj1, proj2 = get_layer('AF_DMN')[1](tparams, options, _p('AF_DMN',num), proj1[0], proj2[0], x1_mask, x2_mask)
         projr1, projr2 = get_layer('AF_DMN')[1](tparams, options, _p('AF_DMN_r',num), projr1[0], projr2[0], xr1_mask, xr2_mask)

    proj3 = proj1
    proj4 = proj2
    projr3 = projr1
    projr4 = projr2
    ctx3 = concatenate([proj3[0], projr3[0][::-1]], axis=proj3[0].ndim - 1)
    logit1 = (ctx3 * x1_mask[:, :, None]).sum(0) / x1_mask.sum(0)[:, None]
    logit2 = (ctx3 * x1_mask[:, :, None]).max(0)

    ctx4 = concatenate([proj4[0], projr4[0][::-1]], axis=proj4[0].ndim - 1)
    logit3 = (ctx4 * x2_mask[:, :, None]).sum(0) / x2_mask.sum(0)[:, None]
    logit4 = (ctx4 * x2_mask[:, :, None]).max(0)

    logit = concatenate([logit1, logit2, logit3, logit4], axis=1)

    if options['use_dropout']:
        logit = dropout_layer(logit, use_noise, trng)

    logit = get_layer('ff')[1](tparams, logit, options,
                               prefix='ff_layer_1', activ='tanh')
    if options['use_dropout']:
        logit = dropout_layer(logit, use_noise, trng)
    logit = get_layer('ff')[1](tparams, logit, options,
                               prefix='ff_layer_output', activ='linear')
    probs = tensor.nnet.softmax(logit)
    cost = tensor.nnet.categorical_crossentropy(probs, y)

    f_pred = theano.function([x1, x1_mask, x2, x2_mask], probs.argmax(axis=1), name='f_pred')

    return trng, use_noise, x1, x1_mask, x2, x2_mask, y, opt_ret, cost, f_pred,


# calculate the log probablities on a given corpus using translation model
def pred_probs(f_log_probs, prepare_data, options, iterator, verbose=False):
    probs = []
    n_done = 0

    for x1, x2, y in iterator:
        n_done += len(x1)
        x1, x1_mask, x2, x2_mask, y = prepare_data(x1, x2, y)

        pprobs = f_log_probs(x1, x1_mask, x2, x2_mask, y)
        for pp in pprobs:
            probs.append(pp)

        if numpy.isnan(numpy.mean(probs)):
            pdb.set_trace()

        if verbose:
            print >> sys.stderr, '%d samples computed' % (n_done)

    return numpy.array(probs)


def pred_acc(f_pred, prepare_data, options, iterator, verbose=False):
    """
    Just compute the accuracy
    f_pred: Theano fct computing the prediction
    prepare_data: usual prepare_data for that dataset.
    """
    valid_acc = 0
    n_done = 0

    for x1, x2, y in iterator:
        n_done += len(x1)
        x1, x1_mask, x2, x2_mask, y = prepare_data(x1, x2, y)
        preds = f_pred(x1, x1_mask, x2, x2_mask)
        valid_acc += (preds == y).sum()

    valid_acc = 1.0 * valid_acc / n_done

    return valid_acc


# optimizers
# name(hyperp, tparams, grads, inputs (list), cost) = f_grad_shared, f_update
def adam(lr, tparams, grads, inp, cost, beta1=0.9, beta2=0.999, e=1e-8):
    gshared = [theano.shared(p.get_value() * 0., name='%s_grad' % k)
               for k, p in tparams.iteritems()]
    gsup = [(gs, g) for gs, g in zip(gshared, grads)]

    f_grad_shared = theano.function(inp, cost, updates=gsup, profile=profile)

    updates = []

    t_prev = theano.shared(numpy.float32(0.))
    t = t_prev + 1.
    lr_t = lr * tensor.sqrt(1. - beta2 ** t) / (1. - beta1 ** t)

    for p, g in zip(tparams.values(), gshared):
        m = theano.shared(p.get_value() * 0., p.name + '_mean')
        v = theano.shared(p.get_value() * 0., p.name + '_variance')
        m_t = beta1 * m + (1. - beta1) * g
        v_t = beta2 * v + (1. - beta2) * g ** 2
        step = lr_t * m_t / (tensor.sqrt(v_t) + e)
        p_t = p - step
        updates.append((m, m_t))
        updates.append((v, v_t))
        updates.append((p, p_t))
    updates.append((t_prev, t))

    f_update = theano.function([lr], [], updates=updates,
                               on_unused_input='ignore', profile=profile)

    return f_grad_shared, f_update


def adadelta(lr, tparams, grads, inp, cost, epsilon=1e-6, rho=0.95):
    zipped_grads = [theano.shared(p.get_value() * numpy.float32(0.),
                                  name='%s_grad' % k)
                    for k, p in tparams.iteritems()]
    running_up2 = [theano.shared(p.get_value() * numpy.float32(0.),
                                 name='%s_rup2' % k)
                   for k, p in tparams.iteritems()]
    running_grads2 = [theano.shared(p.get_value() * numpy.float32(0.),
                                    name='%s_rgrad2' % k)
                      for k, p in tparams.iteritems()]

    zgup = [(zg, g) for zg, g in zip(zipped_grads, grads)]
    rg2up = [(rg2, rho * rg2 + (1 - rho) * (g ** 2))
             for rg2, g in zip(running_grads2, grads)]

    f_grad_shared = theano.function(inp, cost, updates=zgup + rg2up,
                                    profile=profile)

    updir = [-tensor.sqrt(ru2 + epsilon) / tensor.sqrt(rg2 + epsilon) * zg
             for zg, ru2, rg2 in zip(zipped_grads, running_up2,
                                     running_grads2)]
    ru2up = [(ru2, rho * ru2 + (1 - rho) * (ud ** 2))
             for ru2, ud in zip(running_up2, updir)]
    param_up = [(p, p + ud) for p, ud in zip(itemlist(tparams), updir)]

    f_update = theano.function([lr], [], updates=ru2up + param_up,
                               on_unused_input='ignore', profile=profile)

    return f_grad_shared, f_update


def rmsprop(lr, tparams, grads, inp, cost):
    zipped_grads = [theano.shared(p.get_value() * numpy.float32(0.),
                                  name='%s_grad' % k)
                    for k, p in tparams.iteritems()]
    running_grads = [theano.shared(p.get_value() * numpy.float32(0.),
                                   name='%s_rgrad' % k)
                     for k, p in tparams.iteritems()]
    running_grads2 = [theano.shared(p.get_value() * numpy.float32(0.),
                                    name='%s_rgrad2' % k)
                      for k, p in tparams.iteritems()]

    zgup = [(zg, g) for zg, g in zip(zipped_grads, grads)]
    rgup = [(rg, 0.95 * rg + 0.05 * g) for rg, g in zip(running_grads, grads)]
    rg2up = [(rg2, 0.95 * rg2 + 0.05 * (g ** 2))
             for rg2, g in zip(running_grads2, grads)]

    f_grad_shared = theano.function(inp, cost, updates=zgup + rgup + rg2up,
                                    profile=profile)

    updir = [theano.shared(p.get_value() * numpy.float32(0.),
                           name='%s_updir' % k)
             for k, p in tparams.iteritems()]
    updir_new = [(ud, 0.9 * ud - 1e-4 * zg / tensor.sqrt(rg2 - rg ** 2 + 1e-4))
                 for ud, zg, rg, rg2 in zip(updir, zipped_grads, running_grads,
                                            running_grads2)]
    param_up = [(p, p + udn[1])
                for p, udn in zip(itemlist(tparams), updir_new)]
    f_update = theano.function([lr], [], updates=updir_new + param_up,
                               on_unused_input='ignore', profile=profile)

    return f_grad_shared, f_update


def sgd(lr, tparams, grads, inp, cost):
    gshared = [theano.shared(p.get_value() * 0.,
                             name='%s_grad' % k)
               for k, p in tparams.iteritems()]
    gsup = [(gs, g) for gs, g in zip(gshared, grads)]

    f_grad_shared = theano.function(inp, cost, updates=gsup,
                                    profile=profile)

    pup = [(p, p - lr * g) for p, g in zip(itemlist(tparams), gshared)]
    f_update = theano.function([lr], [], updates=pup, profile=profile)

    return f_grad_shared, f_update


"""Note: all the hyperparameters are stored in a dictionary model_options (or options outside train).
   train() then proceeds to do the following:
       1. The params are initialized (or reloaded)
       2. The computations graph is built symbolically using Theano.
       3. A cost is defined, then gradient are obtained automatically with tensor.grad
       4. With some helper functions, gradient descent + periodic saving/printing proceeds
"""


def train(
        dim_word=100,  # word vector dimensionality
        dim=100,  # the number of GRU units
        encoder='lstm',  # encoder model
        decoder='lstm',  # decoder model
        patience=10,  # early stopping patience
        max_epochs=5000,
        finish_after=10000000,  # finish after this many updates
        decay_c=0.,  # L2 regularization penalty
        clip_c=-1.,  # gradient clipping threshold
        lrate=0.01,  # learning rate
        n_words=100000,  # vocabulary size
        maxlen=100,  # maximum length of the description
        optimizer='adadelta',
        batch_size=16,
        valid_batch_size=16,
        saveto='model.npz',
        dispFreq=100,
        validFreq=1000,
        saveFreq=1000,  # save the parameters after every saveFreq updates
        use_dropout=False,
        reload_=False,
        verbose=False,  # print verbose information for debug but slow speed
        datasets=[],
        valid_datasets=[],
        test_datasets=[],
        dictionary='',
        embedding='',  # pretrain embedding file, such as word2vec, GLOVE
        matching_rounds   = 10,
):
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s: %(name)s: %(levelname)s: %(message)s")
    # Model options
    model_options = locals().copy()

    # load dictionary and invert them
    with open(dictionary, 'rb') as f:
        worddicts = pkl.load(f)

    # reload options
    if reload_ and os.path.exists(saveto):
        print 'Reload options'
        with open('%s.pkl' % saveto, 'rb') as f:
            model_options = pkl.load(f)

    logger.debug(pprint.pformat(model_options))

    print 'Loading data'
    train = TextIterator(datasets[0], datasets[1], datasets[2],
                         dictionary,
                         n_words=n_words,
                         batch_size=batch_size)
    train_valid = TextIterator(datasets[0], datasets[1], datasets[2],
                               dictionary,
                               n_words=n_words,
                               batch_size=valid_batch_size,
                               shuffle=False)
    valid = TextIterator(valid_datasets[0], valid_datasets[1], valid_datasets[2],
                         dictionary,
                         n_words=n_words,
                         batch_size=valid_batch_size,
                         shuffle=False)
    test = TextIterator(test_datasets[0], test_datasets[1], test_datasets[2],
                        dictionary,
                        n_words=n_words,
                        batch_size=valid_batch_size,
                        shuffle=False)

    # Initialize (or reload) the parameters using 'model_options'
    # then build the Theano graph
    print 'Building model'
    params = init_params(model_options, worddicts)
    # reload parameters
    if reload_ and os.path.exists(saveto):
        print 'Reload parameters'
        params = load_params(saveto, params)

    # numpy arrays -> theano shared variables
    tparams = init_tparams(params)

    trng, use_noise, \
    x1, x1_mask, x2, x2_mask, y, \
    opt_ret, \
    cost, \
    f_pred = \
        build_model(tparams, model_options)
    inps = [x1, x1_mask, x2, x2_mask, y]

    # before any regularizer
    print 'Building f_log_probs...',
    f_log_probs = theano.function(inps, cost, profile=profile)
    print 'Done'

    cost = cost.mean()

    # apply L2 regularization on weights
    if decay_c > 0.:
        decay_c = theano.shared(numpy.float32(decay_c), name='decay_c')
        weight_decay = 0.
        for kk, vv in tparams.iteritems():
            weight_decay += (vv ** 2).sum()
        weight_decay *= decay_c
        cost += weight_decay

    # after all regularizers - compile the computational graph for cost
    print 'Building f_cost...',
    f_cost = theano.function(inps, cost, profile=profile)
    print 'Done'

    print 'Computing gradient...',
    grads = tensor.grad(cost, wrt=itemlist(tparams))
    print 'Done'

    # apply gradient clipping here
    if clip_c > 0.:
        g2 = 0.
        for g in grads:
            g2 += (g ** 2).sum()
        new_grads = []
        for g in grads:
            new_grads.append(tensor.switch(g2 > (clip_c ** 2),
                                           g / tensor.sqrt(g2) * clip_c,
                                           g))
        grads = new_grads
        if verbose:
            print 'Building function of gradient\'s norm'
            f_norm_g = theano.function(inps, tensor.sqrt(g2))

    # compile the optimizer, the actual computational graph is compiled here
    lr = tensor.scalar(name='lr')
    print 'Building optimizers...',
    f_grad_shared, f_update = eval(optimizer)(lr, tparams, grads, inps, cost)
    print 'Done'

    print 'Optimization'

    history_errs = []
    # reload history
    if reload_ and os.path.exists(saveto):
        print 'Reload history error'
        history_errs = list(numpy.load(saveto)['history_errs'])
    best_p = None
    bad_counter = 0

    if validFreq == -1:
        validFreq = len(train[0]) / batch_size
    if saveFreq == -1:
        saveFreq = len(train[0]) / batch_size

    uidx = 0
    estop = False
    valid_acc_record = []
    test_acc_record = []
    best_epoch_num = 0
    lr_change_list = []
    wait_counter = 0
    wait_N = 1
    for eidx in xrange(max_epochs):
        n_samples = 0
        for x1, x2, y in train:
            n_samples += len(x1)
            uidx += 1
            use_noise.set_value(1.)
            x1, x1_mask, x2, x2_mask, y = prepare_data(x1, x2, y, maxlen=maxlen)

            if x1 is None:
                print 'Minibatch with zero sample under length ', maxlen
                uidx -= 1
                continue

            ud_start = time.time()

            # compute cost, grads and copy grads to shared variables
            cost = f_grad_shared(x1, x1_mask, x2, x2_mask, y)
            if verbose:
                if clip_c > 0.:
                    norm_g = f_norm_g(x1, x1_mask, x2, x2_mask, y)

            # do the update on parameters
            f_update(lrate)
            ud = time.time() - ud_start
            # check for bad numbers, usually we remove non-finite elements
            # and continue training - but not done here
            if numpy.isnan(cost) or numpy.isinf(cost):
                print 'NaN detected'
                return None

            # verbose
            if numpy.mod(uidx, dispFreq) == 0:
                logger.debug('Epoch {0} Update {1} Cost {2} UD {3}'.format(eidx, uidx, cost, ud))
                if verbose:
                    if clip_c > 0.:
                        logger.debug('Grad {0}'.format(norm_g))

            # save the best model so far
            if numpy.mod(uidx, saveFreq) == 0:
                print 'Saving...',
                if best_p is not None:
                    params = best_p
                else:
                    params = unzip(tparams)
                numpy.savez(saveto, history_errs=history_errs, **params)
                pkl.dump(model_options, open('%s.pkl' % saveto, 'wb'))
                print 'Done'

            # validate model on validation set and early stop if necessary
            if numpy.mod(uidx, validFreq) == 0:
                use_noise.set_value(0.)
                valid_cost = pred_probs(f_log_probs, prepare_data, model_options, valid).mean()
                valid_acc = pred_acc(f_pred, prepare_data, model_options, valid)
                valid_err = 1.0 - valid_acc
                history_errs.append(valid_err)
                test_cost = pred_probs(f_log_probs, prepare_data, model_options, test).mean()
                test_acc = pred_acc(f_pred, prepare_data, model_options, test)

                print 'Valid cost', valid_cost
                print 'Valid accuracy', valid_acc
                print 'Test cost', test_cost
                print 'Test accuracy', test_acc
                print 'lrate:', lrate

                valid_acc_record.append(valid_acc)
                test_acc_record.append(test_acc)

                if uidx == 0 or valid_err <= numpy.array(history_errs).min():
                    best_p = unzip(tparams)
                    best_epoch_num = eidx
                    wait_counter = 0

                if valid_err > numpy.array(history_errs).min():
                    wait_counter += 1

                if wait_counter >= wait_N:
                    print 'wait_counter max, need to half the lr'
                    bad_counter += 1
                    wait_counter = 0
                    print 'bad_counter: ' + str(bad_counter)
                    lrate = lrate * 0.5
                    lr_change_list.append(eidx)
                    print 'lrate change to: ' + str(lrate)
                    zipp(best_p, tparams)

                if bad_counter > patience:
                    print 'Early Stop!'
                    estop = True
                    break

                if numpy.isnan(valid_err):
                    pdb.set_trace()

            # finish after this many updates
            if uidx >= finish_after:
                print 'Finishing after %d iterations!' % uidx
                estop = True
                break

        print 'Seen %d samples' % n_samples

        if estop:
            break

    if best_p is not None:
        zipp(best_p, tparams)

    with open('record.csv', 'w') as f:
        f.write(str(best_epoch_num) + '\n')
        f.write(','.join(map(str, lr_change_list)) + '\n')
        f.write(','.join(map(str, valid_acc_record)) + '\n')
        f.write(','.join(map(str, test_acc_record)) + '\n')

    use_noise.set_value(0.)

    print '=' * 80
    print 'Final Result'
    print '=' * 80
    train_cost = pred_probs(f_log_probs, prepare_data, model_options, train_valid).mean()
    train_acc = pred_acc(f_pred, prepare_data, model_options, train_valid)
    print 'Train cost', train_cost
    print 'Train accuracy', train_acc
    valid_cost = pred_probs(f_log_probs, prepare_data, model_options, valid).mean()
    valid_acc = pred_acc(f_pred, prepare_data, model_options, valid)
    print 'Valid cost', valid_cost
    print 'Valid accuracy', valid_acc
    test_cost = pred_probs(f_log_probs, prepare_data, model_options, test).mean()
    test_acc = pred_acc(f_pred, prepare_data, model_options, test)
    print 'Test cost', test_cost
    print 'Test accuracy', test_acc
    params = copy.copy(best_p)
    numpy.savez(saveto, zipped_params=best_p, history_errs=history_errs, **params)
    logger.debug('Done')

    return None


if __name__ == '__main__':
    pass