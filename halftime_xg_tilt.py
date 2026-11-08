"""One-parameter, research-only xG allocation correction of Clubs probabilities.

No I/O or application imports. The single regressor is a fixed contrast of eight
causal, prior-shrunk xG profiles. No intercept, scaling or feature search is fit.
"""
import numpy as np
from scipy.optimize import brentq
from scipy.special import logsumexp

FIRST=(16,17,32,33)
SECOND=(20,21,36,37)
DIRECTION=np.asarray([1.,0.,-1.])
BOUNDS=(-1.,1.)
PENALTY=8.


def indicator(rich):
    rich=np.asarray(rich,dtype=float)
    if rich.ndim!=2 or rich.shape[1]!=48 or not np.isfinite(rich).all():
        raise ValueError('Expected the frozen48-column finite rich matrix.')
    return (rich[:,FIRST].sum(1)-rich[:,SECOND].sum(1))/4.


def _inputs(basep,signal,y=None):
    basep=np.asarray(basep,dtype=float);signal=np.asarray(signal,dtype=float)
    if (basep.ndim!=2 or basep.shape[1]!=3 or not np.isfinite(basep).all()
            or (basep<=0).any() or not np.allclose(basep.sum(1),1.,atol=1e-10,rtol=0)):
        raise ValueError('Positive normalized three-class baseline required.')
    if signal.shape!=(len(basep),) or not np.isfinite(signal).all():raise ValueError('One finite signal per match required.')
    if y is not None:
        y=np.asarray(y)
        if (y.shape!=(len(basep),) or y.dtype.kind not in 'iu' or (y<0).any() or (y>2).any()):
            raise ValueError('Integer labels0,1,2 required.')
    return basep,signal,y


def predict(basep,signal,beta):
    basep,signal,_=_inputs(basep,signal)
    beta=float(beta)
    if not np.isfinite(beta) or not BOUNDS[0]<=beta<=BOUNDS[1]:raise ValueError('Coefficient outside frozen bounds.')
    if beta==0:return basep.copy()
    logits=np.log(basep)+beta*signal[:,None]*DIRECTION
    p=np.exp(logits-logsumexp(logits,axis=1)[:,None])
    p[signal==0]=basep[signal==0]
    if not np.isfinite(p).all() or (p<=0).any():raise ValueError('Invalid corrected distribution.')
    return p


def objective(basep,signal,y,beta,penalty=PENALTY):
    basep,signal,y=_inputs(basep,signal,y)
    beta=float(beta);penalty=float(penalty)
    if not np.isfinite(penalty) or penalty<=0:raise ValueError('Positive finite prior penalty required.')
    if not np.isfinite(beta):raise ValueError('Finite coefficient required.')
    logits=np.log(basep)+beta*signal[:,None]*DIRECTION
    normalizer=logsumexp(logits,axis=1)
    p=np.exp(logits-normalizer[:,None])
    expectation=p@DIRECTION
    value=float((normalizer-logits[np.arange(len(y)),y]).sum()+.5*penalty*beta**2)
    gradient=float(np.sum(signal*(expectation-DIRECTION[y]))+penalty*beta)
    hessian=float(np.sum(signal**2*(p@(DIRECTION**2)-expectation**2))+penalty)
    return value,gradient,hessian


def fit(basep,signal,y,penalty=PENALTY,bounds=BOUNDS):
    basep,signal,y=_inputs(basep,signal,y)
    if not len(y):raise ValueError('At least one fitting match required.')
    low,high=map(float,bounds)
    if not (np.isfinite(low) and np.isfinite(high) and BOUNDS[0]<=low<high<=BOUNDS[1] and low<=0<=high):
        raise ValueError('Bounds must contain zero within[-1,1].')
    at_zero=objective(basep,signal,y,0.,penalty)
    gradient=lambda b:objective(basep,signal,y,b,penalty)[1]
    if at_zero[1]==0:beta=0.
    elif gradient(low)>=0:beta=low
    elif gradient(high)<=0:beta=high
    else:beta=float(brentq(gradient,low,high,xtol=1e-13))
    value,grad,hessian=objective(basep,signal,y,beta,penalty)
    if value>at_zero[0]+1e-8 or hessian<=0:raise RuntimeError('Convex optimization failed.')
    residual=abs(grad) if low<beta<high else max(0.,-grad if beta==low else grad)
    if residual>1e-7:raise RuntimeError('Nonstationary fitted coefficient.')
    return dict(version='xg-allocation-offset-v1',beta=beta,penalty=float(penalty),bounds=[low,high],
        training_matches=len(y),objective=value,zero_objective=at_zero[0],gradient=grad,hessian=hessian,kkt_residual=residual,
        signal_min=float(signal.min()),signal_max=float(signal.max()),signal_mean=float(signal.mean()),signal_std=float(signal.std()))
