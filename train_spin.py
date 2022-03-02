from time import sleep
import jax
import jax.numpy as jnp                # JAX NumPy
from jax import grad, jacfwd, jacrev

from flax import linen as nn           # The Linen API
# Useful dataclass to keep train weight_dict
from flax.training import train_state

import numpy as np                     # Ordinary NumPy
import optax                           # Optimizers
from backbone import EigenNet
from physics import hamiltonian_operator
from helper import moving_average
import sys
from flax.core import FrozenDict
import time

def create_train_state(n_dense_neurons, n_eigenfuncs, batch_size, D, learning_rate, decay_rate, sparsifying_K, n_space_dimension=2, init_rng=0):
    model = EigenNet(
        features=[n_dense_neurons, n_dense_neurons, n_dense_neurons, n_eigenfuncs], D=D)
    batch = jnp.ones((batch_size, n_space_dimension))
    weight_dict = model.init(init_rng, batch)
    layer_sparsifying_masks = EigenNet.get_all_layer_sparsifying_masks(
        weight_dict, sparsifying_K)
    weight_dict = EigenNet.sparsify_weights(
        weight_dict, layer_sparsifying_masks)

    """Creates initial `TrainState`."""
    opt = optax.rmsprop(learning_rate, decay_rate)
    opt_state = opt.init(weight_dict)
    return model, weight_dict, opt, opt_state, layer_sparsifying_masks


def get_network_as_function_of_input(model, params):
    return lambda batch: model.apply(params, batch)


def get_network_as_function_of_weights(model, batch):
    return lambda weights: model.apply(weights, batch)

# This jit seems not making any difference
def calculate_masked_gradient(del_u_del_weights, pred, h_u, sigma_t_bar, moving_average_beta):
    sigma_t_hat = jnp.mean(
        pred[:, :, None]@pred[:, :, None].swapaxes(2, 1), axis=0)

    pi_t_hat = jnp.mean(
        h_u[:, :, None]@pred[:, :, None].swapaxes(2, 1), axis=0)

    sigma_t_bar = moving_average(
        sigma_t_bar, sigma_t_hat, beta=moving_average_beta)

    L = jnp.linalg.cholesky(sigma_t_bar)
    L_inv = jnp.linalg.inv(L)
    L_inv_T = L_inv.T
    L_diag_inv = jnp.eye(L.shape[0]) * (1/jnp.diag(L))

    A_1 = L_inv_T @ L_diag_inv
    A_1 = h_u @ A_1

    Lambda = L_inv @ pi_t_hat @ L_inv_T
    A_2 = L_inv_T @ jnp.triu(Lambda @ L_diag_inv)
    A_2 = pred @ A_2

    for key in del_u_del_weights['params'].keys():
        j_pi_t_hat = jnp.einsum('bj, bjcw -> bcw', A_1,
                                del_u_del_weights['params'][key]['kernel'])
        j_pi_t_hat = jnp.mean(j_pi_t_hat, axis=0)

        j_sigma_t_hat = jnp.einsum(
            'bj, bjcw -> bcw', A_2,  del_u_del_weights['params'][key]['kernel'])
        j_sigma_t_hat = jnp.mean(j_sigma_t_hat, axis=0)
        j_sigma_t_bar[key] = moving_average(
            j_sigma_t_bar[key], j_sigma_t_hat, moving_average_beta)

        masked_grad = j_pi_t_hat - j_sigma_t_bar[key]
        del_u_del_weights['params'][key]['kernel'] = masked_grad

    return FrozenDict(del_u_del_weights)


def train_step(model, weight_dict, opt, opt_state, batch, sigma_t_bar, j_sigma_t_bar, moving_average_beta):
    t1 = time.time()
    u_of_x = get_network_as_function_of_input(model, weight_dict)
    u_of_w = get_network_as_function_of_weights(model, batch)
    print('Create functions ', time.time() - t1)

    t1 = time.time()
    pred = u_of_x(batch)
    print('Single predictions ', time.time() - t1)

    t1 = time.time()
    del_u_del_weights = jacrev(u_of_w)(weight_dict)
    print('Create functions ', time.time() - t1)

    t1 = time.time()
    h_u = hamiltonian_operator(u_of_x, batch, system='hydrogen')
    print('Hamiltonian ', time.time() - t1)
    t1 = time.time()

    masked_gradient = calculate_masked_gradient(del_u_del_weights, pred, h_u, sigma_t_bar, moving_average_beta)
    print('Masked gradients ', time.time()-t1)
    t1 = time.time()

    weight_dict = FrozenDict(weight_dict)
    updates, opt_state = opt.update(masked_gradient, opt_state)
    weight_dict = optax.apply_updates(weight_dict, updates)
    print('Freeze Dicts ', time.time()-t1)
    print()

    # TODO how do we get the energies?
    energies = 0

    return weight_dict, energies, sigma_t_bar, j_sigma_t_bar


if __name__ == '__main__':
    rng = jax.random.PRNGKey(0)
    rng, init_rng = jax.random.split(rng)

    # Hyperparameter
    # Network parameter
    sparsifying_K = 3
    n_dense_neurons = 128
    n_eigenfuncs = 9

    # Optimizer
    learning_rate = 1e-5
    decay_rate = 0.999
    moving_average_beta = 0.01

    # Train setup
    num_epochs = 100
    batch_size = 100

    # Simulation size
    D = 50

    # Create initial state
    model, weight_dict, opt, opt_state, layer_sparsifying_masks = create_train_state(
        n_dense_neurons, n_eigenfuncs, batch_size, D, learning_rate, decay_rate, sparsifying_K, init_rng=init_rng)
    weight_dict = weight_dict.unfreeze()
    sigma_t_bar = jnp.eye(n_eigenfuncs)
    j_sigma_t_bar = {key: jnp.zeros_like(
        weight_dict['params'][key]['kernel']) for key in weight_dict['params'].keys()}

    t1 = time.time()
    for epoch in range(1, num_epochs + 1):
        batch = jax.random.uniform(
            rng, minval=-D, maxval=D, shape=(batch_size, 2))

        # Run an optimization step over a training batch
        weight_dict, energies, sigma_t_bar, j_sigma_t_bar = train_step(
            model, weight_dict, opt, opt_state, batch, sigma_t_bar, j_sigma_t_bar, moving_average_beta)
        weight_dict = EigenNet.sparsify_weights(
            weight_dict, layer_sparsifying_masks)
        weight_dict = weight_dict.unfreeze()
        print(epoch)
    print('Time ', time.time()-t1)
    # 39.507134199142456
