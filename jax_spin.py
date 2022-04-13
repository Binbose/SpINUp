import time

import jax.numpy as np
from jax import random, jit, vmap, jacfwd
from jax.experimental import optimizers
from jax.nn import sigmoid, softplus
from jax import tree_multimap
from jax import ops

import itertools
from functools import partial
from torch.utils import data
from tqdm import trange
import matplotlib.pyplot as plt
from jax.config import config
config.update("jax_enable_x64", True)

def MLP(layers):
    def init(rng_key):
        def init_layer(key, d_in, d_out):
            k1, k2 = random.split(key)
            W = random.normal(k1, (d_in, d_out))
            b = random.normal(k2, (d_out,))
            return W, b
        key, *keys = random.split(rng_key, len(layers))
        params = list(map(init_layer, keys, layers[:-1], layers[1:]))
        return params
    def apply(params, inputs):
        for W, b in params[:-1]:
            outputs = np.dot(inputs, W) #+ b
            inputs = sigmoid(outputs)
        W, b = params[-1]
        outputs = np.dot(inputs, W) #+ b
        return outputs
    return init, apply

class Sampler:
    # Initialize the class
    def __init__(self, dim, coords, name = None):
        self.dim = dim
        self.coords = coords
        self.name = name

    def sample(self, N, key = random.PRNGKey(1239)):
        x = self.coords.min(1) + (self.coords.max(1)-self.coords.min(1))*random.uniform(key, (N, self.dim))
        return x


class DataGenerator(data.Dataset):
    def __init__(self, dom_sampler, batch_size=64):
        'Initialization'
        self.dom_sampler = dom_sampler
        self.batch_size = batch_size
        self.key = random.PRNGKey(10001)

    def __getitem__(self, index):
        'Generate one batch of data'
        self.key, subkey = random.split(self.key)
        X = self.__data_generation(subkey)
        return X

    @partial(jit, static_argnums=(0,))
    def __data_generation(self, key):
        'Generates data containing batch_size samples'
        inputs = self.dom_sampler.sample(self.batch_size, key)
        return inputs

class SpIN:
    # Initialize the class
    def __init__(self, operator, layers):
        
        # Callable operator function
        self.operator = operator
          
        # Network initialization and evaluation functions
        self.net_init, self.net_apply = MLP(layers)
        
        # Initialize network parameters
        params = self.net_init(random.PRNGKey(2))
        np.save('./weights', params)

        # Optimizer initialization and update functions
        #lr = optimizers.exponential_decay(1e-4, decay_steps=1000, decay_rate=0.9)
        lr = 1e-4
        self.opt_init, self.opt_update, self.get_params = optimizers.rmsprop(lr)
        self.opt_state = self.opt_init(params)
          
        # Decay parameter
        self.beta = 1

        # Number of eigenvalues
        self.neig = layers[-1]

        # Logger
        self.itercount = itertools.count()
        self.loss_log = []
        self.evals_log = []
        
    def apply_mask(self, inputs, outputs):
        # mask is used to zero the boundary points.
        mask = 0.1
        if len(inputs.shape) == 2:
            for i in range(inputs.shape[1]):
                mask *= np.maximum((-inputs[:,i]**2 + np.pi * inputs[:,i]), 0)
            mask = np.expand_dims(mask, -1)

        elif len(inputs.shape) == 1:
            for x in inputs:
                mask *= np.maximum((-x ** 2 + np.pi * x ), 0)

        return mask*outputs
        
    def net_u(self, params, inputs):
        outputs = self.net_apply(params, inputs)
        outputs = self.apply_mask(inputs, outputs)
        return outputs
    
    def evaluate_spin(self, params, inputs, averages, beta, epoch=0):
        # Fetch batch
        n = inputs.shape[0]
        sigma_avg, _ = averages
        
        # Evaluate model
        u = self.net_u(params, inputs)

        sigma = np.dot(u.T, u)/n
        sigma_avg = (1.0 - beta) * sigma_avg + beta * sigma # $\bar{\Sigma}$

        # Cholesky
        chol = np.linalg.cholesky(sigma_avg)
        choli = np.linalg.inv(chol) # $L^{-1}$

        # Operator
        operator = self.operator(self.net_u, params, inputs)
        pi = np.dot(operator.T, u)/n # $\Pi$
        rq = np.dot(choli, np.dot(pi, choli.T)) # $\Lambda$


        return (u, choli, pi, rq, operator), sigma_avg
    
    def masked_gradients(self, params, inputs, outputs, averages, beta):
        # Fetch batch
        n = inputs.shape[0]
        u, choli, _, rq, operator = outputs
        _, sigma_jac_avg = averages
        
        dl = np.diag(np.diag(choli))
        triu = np.triu(np.dot(rq, dl))

        grad_sigma = -1.0 * np.matmul(choli.T, triu) # \frac{\partial tr(\Lambda)}{\partial \Sigma}
        grad_pi = np.dot(choli.T, dl) # \frac{\partail tr(\Lambda){\partial \Pi}}

        grad_param_pre = jacfwd(self.net_u) 
        grad_param = vmap(grad_param_pre, in_axes = (None, 0))

        grad_theta = grad_param(params, inputs) # \frac{\partial u}{\partial \theta}

        sigma_jac = tree_multimap(lambda x: 
                                  np.tensordot(u.T, x, 1), 
                                  grad_theta) # frac{\partail \Sigma}{\partial \theta}
  
        sigma_jac_avg = tree_multimap(lambda x,y: 
                                      (1.0-beta) * x + beta * y, 
                                      sigma_jac_avg, 
                                      sigma_jac)
    
        gradient_pi_1 = np.dot(grad_pi.T, operator.T)
  
        # gradient  = \frac{\partial tr(\Lambda)}{\partial \theta}
        gradients = tree_multimap(lambda x,y: 
                                  (np.tensordot(gradient_pi_1, x, ([0,1],[1,0])) + 
                                   1.0 * np.tensordot(grad_sigma.T, y,([0,1],[1,0])))/n,
                                  grad_theta, 
                                  sigma_jac_avg)
        # Negate for gradient ascent
        gradients = tree_multimap(lambda x: -1.0*x, gradients) 
                       
        return gradients, sigma_jac_avg
    
    def loss_and_grad(self, params, batch, epoch=0):
        # Fetch batch
        inputs, averages, beta = batch
        
        # Evaluate SPIN model
        outputs, sigma_avg = self.evaluate_spin(params, 
                                                inputs, 
                                                averages, 
                                                beta,
                                                epoch=epoch)


        # Compute loss
        _, _, _, rq, _ = outputs
        eigenvalues = np.diag(rq)  # eigenvalues are the diagonal entries of $\Lamda$
        loss = np.sum(eigenvalues)

        # Compute masked gradients
        gradients, sigma_jac_avg = self.masked_gradients(params, 
                                                         inputs, 
                                                         outputs, 
                                                         averages, 
                                                         beta)
        
        # Store updated averages
        averages = (sigma_avg, sigma_jac_avg)
        
        return loss, gradients, averages
    
    def init_sigma_jac(self, params, inputs):
        u = model.net_u(params, inputs)
        grad_param = jacfwd(model.net_u) 
        grad_theta = grad_param(params, inputs)

        sigma_jac = tree_multimap(lambda x: np.tensordot(u.T, x, 1)*0,
                                          grad_theta)

        return sigma_jac

    # Define a jit-compiled update step
    @partial(jit, static_argnums=(0,))
    def step(self, i, opt_state, batch):
        params = self.get_params(opt_state)
        loss, gradients, averages = self.loss_and_grad(params, batch, epoch=i)

        opt_state = self.opt_update(i, gradients, opt_state)

        return loss, opt_state, averages
    
    # Optimize parameters in a loop
    def train(self, dataset, nIter = 10000):
        inputs = iter(dataset)
        pbar = trange(nIter)
        
        # Initialize moving averages
        sigma_avg = np.ones(self.neig)
        sigma_jac_avg = self.init_sigma_jac(self.get_params(self.opt_state), next(inputs))
        averages = (sigma_avg, sigma_jac_avg)
        
        # Main training loop
        for it in pbar:
            # Set beta
            cnt = next(self.itercount)
            beta = self.beta if cnt > 0 else 1.0
                        
            # Create batch
            batch = next(inputs), averages, beta
            
            # Run one gradient descent update
            loss, self.opt_state, averages = self.step(cnt, self.opt_state, batch)
            
            # Logger
            params = self.get_params(self.opt_state)
            evals, _ = self.eigenpairs(params, next(inputs), averages, beta)
            self.loss_log.append(loss)
            self.evals_log.append(evals)
            pbar.set_postfix({'Loss': loss})
          
        return params, averages, beta
            
            
    # Evaluates predictions at test points  
    @partial(jit, static_argnums=(0,))
    def eigenpairs(self, params, inputs, averages, beta):
        outputs, _ = self.evaluate_spin(params, inputs, averages, beta)
        u, choli, _, rq, _ = outputs
        evals = np.diag(rq) 
        efuns = np.matmul(u, choli.T)
        return evals, efuns


def laplacian_1d(u_fn, params, inputs):
    def action(params, inputs):
        u_xx = jacfwd(jacfwd(u_fn, 1), 1)(params, inputs)
        return u_xx
    vec_fun = vmap(action, in_axes = (None, 0))
    laplacian = vec_fun(params, inputs)

    return np.squeeze(laplacian)

def laplacian_2d(u_fn, params, inputs):
    fun = lambda params,x,y: u_fn(params, np.array([x,y]))
    def action(params,x,y):
        u_xx = jacfwd(jacfwd(fun, 1), 1)(params,x,y)
        u_yy = jacfwd(jacfwd(fun, 2), 2)(params,x,y)
        return u_xx + u_yy
    vec_fun = vmap(action, in_axes = (None, 0, 0))
    laplacian = vec_fun(params, inputs[:,0], inputs[:,1])
    return laplacian


# Exact solution in [0, pi]
def exact_eigenpairs_1d(x, n):
    idx = np.arange(n)+1
    evals = -idx**2
    efuns = np.sqrt(2.0/np.pi) * np.sin(idx * x)
    return evals, efuns


# Problem setup
ndim = 2
neig = 4

# Domain boundaries
dom_coords = np.array([[0, np.pi]])

# Create data sampler
dom_sampler = Sampler(ndim, dom_coords)

dataset = DataGenerator(dom_sampler, batch_size=128)

# Test data
n_star = 100
x_star = np.linspace(0.0, np.pi, n_star)[:,None]


layers = [ndim, 64, 64, 64, 32, neig]
model = SpIN(laplacian_1d, layers)

opt_params, averages, beta = model.train(dataset, nIter=3000)


evals, efuns = model.eigenpairs(opt_params, x_star, averages, beta)
print('Predicted eigenvalues: {}'.format(evals))
evals_true, efuns_true = exact_eigenpairs_1d(x_star, neig)
print('True eigenvalues: {}'.format(evals_true))



plt.figure(figsize=(24,6))
plt.subplot(1, 3, 1)
plt.plot(x_star, efuns)
plt.xlabel('x')
plt.ylabel('u(x)')
plt.title('Predicted eigenfunctions')
plt.tight_layout()
plt.subplot(1,3,2)
plt.plot(x_star, efuns_true)
plt.xlabel('x')
plt.ylabel('u(x)')
plt.title('True eigenfunctions')
plt.tight_layout()
plt.subplot(1,3,3)
plt.plot(model.evals_log)
for i in range(neig):
  plt.axhline(evals_true[i], color='black')
plt.xlabel('iteration')
plt.ylabel('$\lambda$')
plt.tight_layout()

plt.figure()
plt.plot(model.loss_log)
plt.axhline(-30.0, color='black')
plt.xlabel('iteration')
plt.ylabel('Loss')
plt.tight_layout()

plt.show()