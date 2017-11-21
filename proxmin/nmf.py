from __future__ import print_function, division
import logging
import numpy as np
from . import operators
from . import utils
from . import algorithms

logging.basicConfig()
logger = logging.getLogger("proxmin.nmf")

def delta_data(A, S, Y, W=1):
    return W*(A.dot(S) - Y)

def grad_likelihood_A(A, S, Y, W=1):
    D = delta_data(A, S, Y, W=W)
    return D.dot(S.T)

def grad_likelihood_S(S, A, Y, W=1):
    D = delta_data(A, S, Y, W=W)
    return A.T.dot(D)

# executes one proximal step of likelihood gradient, followed by prox_g
def prox_likelihood_A(A, step, S=None, Y=None, prox_g=None, W=1):
    return prox_g(A - step*grad_likelihood_A(A, S, Y, W=W), step)

def prox_likelihood_S(S, step, A=None, Y=None, prox_g=None, W=1):
    return prox_g(S - step*grad_likelihood_S(S, A, Y, W=W), step)

def prox_likelihood(X, step, Xs=None, j=None, Y=None, W=None,
                    prox_S=operators.prox_id, prox_A=operators.prox_id):
    if j == 0:
        return prox_likelihood_A(X, step, S=Xs[1], Y=Y, prox_g=prox_A, W=W)
    else:
        return prox_likelihood_S(X, step, A=Xs[0], Y=Y, prox_g=prox_S, W=W)

class Steps_AS:
    def __init__(self, slack=0.9, Wmax=None, max_stride=100, update_order=None, WAmax=None, WSmax=None):
        """Helper class to compute the Lipschitz constants of grad f.

        Because the spectral norm is expensive to compute, it will only update
        the step_size if relative changes of L exceed (1-slack)/2.
        If not, which is usually the case after only a few iterations, it will
        report a previous value for the next several iterations. The stride
        beteen updates is set by
            stride -> stride * (1-slack)/2 / rel_error
        i.e. it increases more strongly if the rel_error is much below the
        slack budget.
        """
        assert slack > 0 and slack <= 1

        self.slack = slack
        if WAmax is None:
            if Wmax is None:
                WAmax = Wmax = 1
            else:
                WAmax = Wmax
        if WSmax is None:
            if Wmax is None:
                WSmax = Wmax = 1
            else:
                WSmax = Wmax
        self.WSmax = WSmax
        self.WAmax = WAmax
        self.max_stride = max_stride
        # need to knwo when to advance the iterations counter
        if update_order is None:
            self.advance_index = 1
        else:
            self.advance_index = update_order[-1]

        self.it = 0
        N = 2
        self.stride = [1] * N
        self.last = [-1] * N
        self.stored = [None] * N # last update of L

    def __call__(self, j, Xs):
        if self.it >= self.last[j] + self.stride[j]:
            self.last[j] = self.it
            if j == 0:
                L = utils.get_spectral_norm(Xs[1].T) * self.WAmax  # ||S*S.T||
            else:
                L = utils.get_spectral_norm(Xs[0]) * self.WSmax # ||A.T * A||
            if j == self.advance_index:
                self.it += 1

            # increase stride when rel. changes in L are smaller than (1-slack)/2
            if self.it > 1 and self.slack < 1:
                rel_error = np.abs(self.stored[j] - L) / self.stored[j]
                budget = (1-self.slack)/2
                if rel_error < budget and rel_error > 0:
                    self.stride[j] += max(1,int(budget/rel_error * self.stride[j]))
                    self.stride[j] = min(self.max_stride, self.stride[j])
            # updated last value
            self.stored[j] = L
        elif j == self.advance_index:
            self.it += 1

        return self.slack / self.stored[j]

def nmf(Y, A0, S0, W=None, prox_A=operators.prox_plus, prox_S=operators.prox_plus, proxs_g=None, steps_g=None, Ls=None, slack=0.9, update_order=None, steps_g_update='steps_f', accelerated=False, max_iter=1000, e_rel=1e-3, e_abs=0, traceback=False):
    """Non-negative matrix factorization.

    This method solves the NMF problem
        minimize || Y - AS ||_2^2
    under an arbitrary number of constraints and A and/or S.

    Args:
        Y:  target matrix MxN
        A0: initial amplitude matrix MxK
        S0: initial source matrix KxN
        W: (optional weight matrix MxN)
        prox_A: direct projection contraint of A
        prox_S: direct projection constraint of S
        proxs_g: list of constraints for A or S for ADMM-type optimization
            [[prox_A_0, prox_A_1...],[prox_S_0, prox_S_1,...]]
        steps_g: specific value of step size for proxs_g (experts only!)
        Ls: list of linear operators for the constraint functions proxs_g
            If set, needs to have same format as proxs_g.
            Matrices can be numpy.array, scipy.sparse, or None (for identity).
        slack: tolerance for (re)evaluation of Lipschitz constants
            See Steps_AS() for details.
        update_order: list of factor indices in update order
            j=0 -> A, j=1 -> S
        accelerated: If Nesterov acceleration should be used for A and S
        max_iter: maximum iteration number, irrespective of current residuals
        e_rel: relative error threshold for primal and dual residuals
        e_abs: absolute error threshold for primal and dual residuals
        traceback: whether a record of all optimization variables is kept

    Returns:
        A, S: updated amplitude and source matrices
        A, S, trace: adds utils.Traceback if traceback is True

    See also:
        algorithms.bsdmm for step sizes and update sequences
        utils.AcceleratedProxF for Nesterov acceleration

    Reference:
        Moolekamp & Melchior, 2017 (arXiv:1708.09066)

    """

    # create stepsize callback, needs max of W
    if W is not None:
        Wmax = W.max()
    else:
        W = Wmax = 1
    steps_f = Steps_AS(Wmax=Wmax, slack=slack, update_order=update_order)

    # gradient step, followed by direct application of prox_S or prox_A
    from functools import partial
    f = partial(prox_likelihood, Y=Y, W=W, prox_S=prox_S, prox_A=prox_A)

    Xs = [A0, S0]
    res = algorithms.bsdmm(Xs, f, steps_f, proxs_g, steps_g=steps_g, Ls=Ls,
                           update_order=update_order, steps_g_update=steps_g_update, accelerated=accelerated,
                           max_iter=max_iter, e_rel=e_rel, e_abs=e_abs, traceback=traceback)

    if not traceback:
        return res[0], res[1]
    else:
        return res[0][0], res[0][1], res[1]
