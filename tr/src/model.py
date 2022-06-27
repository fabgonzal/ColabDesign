import random
import numpy as np
import jax
import jax.numpy as jnp

from tr.src.utils import update_dict
from tr.src.trrosetta import TrRosetta, get_model_params

# borrow some stuff from AfDesign
from af.src.misc import _np_get_6D
from af.src.prep import prep_pdb, prep_pos
from alphafold.common import protein, residue_constants
ORDER_RESTYPE = {v: k for k, v in residue_constants.restype_order.items()}

class mk_trdesign_model():
  def __init__(self, protocol="fixbb", model_num=1, model_sample=True, data_dir="."):
    
    assert protocol in ["fixbb","hallucination","partial"]

    self.protocol = protocol
    self._data_dir = data_dir

    # set default options
    self.opt = {"temp":1.0, "soft":1.0, "hard":1.0,
                "model":{"num":model_num,"sample":model_sample},
                "weights":{}}
                
    self.params = {"seq":None}

    # setup model
    self._runner = TrRosetta()
    self._model_params = [get_model_params(f"{data_dir}/models/model_xa{k}.npy") for k in list("abcde")]
    self._grad_fn, self._fn = [jax.jit(x) for x in self._get_model()]

    if protocol in ["hallucination","partial"]:
      self.bkg_model = TrRosetta(bkg_model=True)
  
  def _get_model(self):

    def _get_seq(params, opt):
      seq = {"input":params["seq"]}

      # straight-through/reparameterization
      seq["logits"] = 2.0 * seq["input"]
      seq["soft"] = jax.nn.softmax(seq["logits"] / opt["temp"])
      seq["hard"] = jax.nn.one_hot(seq["soft"].argmax(-1), 20)
      seq["hard"] = jax.lax.stop_gradient(seq["hard"] - seq["soft"]) + seq["soft"]

      # create pseudo sequence
      seq["pseudo"] = opt["soft"] * seq["soft"] + (1-opt["soft"]) * seq["input"]
      seq["pseudo"] = opt["hard"] * seq["hard"] + (1-opt["hard"]) * seq["pseudo"]

      if self.protocol in ["partial"] and "pos" in opt:
        pos = opt["pos"]
        seq_ref = jax.nn.one_hot(self._batch["aatype"],20)
        seq = jax.tree_map(lambda x:x.at[pos,:].set(seq_ref), seq)
      return seq
    
    def _get_loss(outputs, opt):
      aux = {"outputs":outputs, "losses":{}}
      log_p = jax.tree_map(jax.nn.log_softmax, outputs)

      # bkg loss
      if self.protocol in ["hallucination","partial"]:
        p = jax.tree_map(jax.nn.softmax, outputs)
        log_q = jax.tree_map(jax.nn.log_softmax, self.bkg_feats)
        aux["losses"]["bkg"] = {}
        for k in ["dist","omega","theta","phi"]:
          aux["losses"]["bkg"][k] = -(p[k]*(log_p[k]-log_q[k])).sum(-1).mean()

      # cce loss
      if self.protocol in ["fixbb","partial"]:
        if self.protocol in ["partial"] and "pos" in opt:
          pos = opt["pos"]
          log_p = jax.tree_map(lambda x:x[:,pos][pos,:], log_p)

        q = self.feats
        aux["losses"]["cce"] = {}
        for k in ["dist","omega","theta","phi"]:
          aux["losses"]["cce"][k] = -(q[k]*log_p[k]).sum(-1).mean()

      # weighted loss
      weighted_losses = jax.tree_map(lambda l, w: l * w, aux["losses"],opt["weights"])
      loss = jax.tree_leaves(weighted_losses)
      return sum(loss), aux

    def _model(params, model_params, opt):
      seq = _get_seq(params, opt)
      outputs = self._runner(seq["pseudo"], model_params)

      loss, aux = _get_loss(outputs, opt)
      return loss, aux

    return jax.value_and_grad(_model, has_aux=True, argnums=0), _model
  
  def prep_inputs(self, pdb_filename=None, chain=None, length=None, pos=None,
                  **kwargs):
    
    if self.protocol in ["fixbb", "partial"]:
      # parse PDB file and return features compatible with TrRosetta
      pdb = prep_pdb(pdb_filename, chain, for_alphafold=False)
      self._batch = pdb["batch"]

      if self.protocol in ["partial"] and pos is not None:
        p = prep_pos(pos, **pdb["idx"])
        self._batch = jax.tree_map(lambda x:x[p], self._batch)
        self.opt["pos"] = np.arange(len(p))

      self.feats = _np_get_6D_binned(self._batch["all_atom_positions"],
                                     self._batch["all_atom_mask"])

      self._len = len(self._batch["aatype"])
      self.opt["weights"]["cce"] = {"dist":1/6,"omega":1/6,"theta":2/6,"phi":2/6}

    if self.protocol in ["hallucination", "partial"]:
      # compute background distribution
      if length is not None: self._len = length
      self.bkg_feats = []
      key = jax.random.PRNGKey(0)
      for n in range(1,6):
        model_params = get_model_params(f"{self._data_dir}/bkgr_models/bkgr0{n}.npy")
        self.bkg_feats.append(self.bkg_model(model_params, key, self._len))
      self.bkg_feats = jax.tree_map(lambda *x:jnp.stack(x).mean(0), *self.bkg_feats)
      self.opt["weights"]["bkg"] = {"dist":1/6,"omega":1/6,"theta":2/6,"phi":2/6}

    self.restart(**kwargs)
  
  def set_weights(self, *args, **kwargs):
    update_dict(self.opt["weights"], *args, **kwargs)

  def set_opt(self, *args, **kwargs):
    update_dict(self.opt, *args, **kwargs)

  def restart(self, seed=None, opt=None, weights=None):
    self._seed = random.randint(0,2147483647) if seed is None else seed
    self._key = jax.random.PRNGKey(self._seed)
    self.params["seq"] = np.zeros((self._len,20))
    self.set_opt(opt)
    self.set_weights(weights)
    
  def run(self, seq=None, params=None, opt=None, weights=None, backprop=True):
    '''run model to get outputs, losses and gradients'''

    # override settings if defined
    update_dict(self.params, seq=seq)
    update_dict(self.params, params)
    self.set_opt(opt)
    self.set_weights(weights)
    
    # decide which model params to use
    m = self.opt["model"]["num"]
    ns = jnp.arange(5)
    if self.opt["model"]["sample"] and m != len(ns):
      self._key, key = jax.random.split(self._key)
      model_num = jax.random.choice(key,ns,(m,),replace=False)
    else:
      model_num = ns[:m]
    model_num = np.array(model_num).tolist()

    # run in serial
    _loss, _aux, _grad = [],[],[]
    for n in model_num:
      model_params = self._model_params[n]
      if backprop:
        (l,a),g = self._grad_fn(self.params, model_params, self.opt)
        _grad.append(g)
      else:
        l,a = self._fn(self.params, model_params, self.opt)
      _loss.append(l)
      _aux.append(a)
    
    # average results
    if len(model_num) > 1:
      _loss = jnp.asarray(_loss).mean()
      _aux = jax.tree_map(lambda *v: jnp.stack(v).mean(0), *_aux)
      if backprop: _grad = jax.tree_map(lambda *v: jnp.stack(v).mean(0), *_grad)
    else:
      _loss,_aux = _loss[0],_aux[0]
      if backprop: _grad = _grad[0] 

    if not backprop:
      _grad = jax.tree_map(jnp.zeros_like, self.params)

    # update
    self._loss = _loss
    self._aux = _aux
    self._aux["model_num"] = model_num
    self._grad = _grad
  
  def af_callback(self, weight=1.0, add_loss=True):
    backprop = weight > 0      
    def get_loss(k):
      losses = self._aux["losses"][k]
      weights = self.opt["weights"][k]
      weighted_losses = jax.tree_map(lambda l,w:l*w, losses, weights)
      return sum(jax.tree_leaves(weighted_losses))
    def callback(af_model):
      for k in ["soft","temp","hard","pos"]:
        if k in self.opt and k in af_model.opt:
          self.opt[k] = af_model.opt[k]
      seq = af_model.params["seq"][0]
      self.run(seq=seq, backprop=backprop)
      if backprop:
        af_model._grad["seq"] += weight * self._grad["seq"]
      if add_loss:
        af_model._loss += weight * self._loss
      if self.protocol in ["hallucination","partial"]:
        af_model._aux["losses"]["TrD_bkg"] = get_loss("bkg")
      if self.protocol in ["fixbb","partial"]:
        af_model._aux["losses"]["TrD_cce"] = get_loss("cce")
      
    return callback

def _np_get_6D_binned(all_atom_positions, all_atom_mask):
  # TODO: make differentiable, add use_jax option
  ref = _np_get_6D(all_atom_positions,
                   all_atom_mask,
                   use_jax=False, for_trrosetta=True)
  ref = jax.tree_map(jnp.squeeze,ref)

  def mtx2bins(x_ref, start, end, nbins, mask):
    bins = np.linspace(start, end, nbins)
    x_true = np.digitize(x_ref, bins).astype(np.uint8)
    x_true = np.where(mask,0,x_true)
    return np.eye(nbins+1)[x_true][...,:-1]

  mask = (ref["dist"] > 20) | (np.eye(ref["dist"].shape[0]) == 1)
  return {"dist": mtx2bins(ref["dist"],    2.0,  20.0,  37,  mask=mask),
          "omega":mtx2bins(ref["omega"], -np.pi, np.pi, 25,  mask=mask),
          "theta":mtx2bins(ref["theta"], -np.pi, np.pi, 25,  mask=mask),
          "phi":  mtx2bins(ref["phi"],      0.0, np.pi, 13,  mask=mask)}
