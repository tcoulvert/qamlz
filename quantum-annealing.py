#!/usr/bin/env python3

import datetime
import json
import os
import sys
import time

import numpy as np
from scipy.optimize import basinhopping
from sklearn.metrics import accuracy_score

from contextlib import closing

from dimod import fix_variables, BinaryQuadraticModel as BQM
from multiprocessing import Pool
from dwave.cloud import Client 
from minorminer import find_embedding
from dwave.embedding import embed_ising, unembed_sampleset
from dwave.embedding import chain_breaks, is_valid_embedding, verify_embedding
from dwave.system.samplers import DWaveSampler
from networkx import Graph

script_path = os.path.dirname(os.path.realpath(__file__))
timestamp = datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')

a_time = 5
# train_sizes = [100, 1000, 5000, 10000, 15000, 20000]
train_sizes = [100, 1000]
start_num = 9
end_num = 10

rng = np.random.default_rng(0)

zoom_factor = 0.5
n_iterations = 4

flip_probs = np.array([0.16, 0.08, 0.04, 0.02] + [0.01]*(n_iterations - 4))
flip_others_probs = np.array([0.16, 0.08, 0.04, 0.02] + [0.01]*(n_iterations - 4))/2
flip_state = -1

AUGMENT_CUTOFF_PERCENTILE = 95
AUGMENT_SIZE = 7   # must be an odd number (since augmentation includes original value in middle)
AUGMENT_OFFSET = 0.0075

FIXING_VARIABLES = True

test_results = {
    'train_sizes': train_sizes,
    'zoom_factor': zoom_factor,
    'n_iterations': n_iterations,
    'AUGMENT_CUTOFF_PERCENTILE': AUGMENT_CUTOFF_PERCENTILE,
    'AUGMENT_SIZE': AUGMENT_SIZE,
    'AUGMENT_OFFSET': AUGMENT_OFFSET,
    'results': []
}

class DWavePlatform:
    PEGASUS = 1
    CHIMERA = 2

platform = DWavePlatform.PEGASUS

url = "https://cloud.dwavesys.com/sapi/"
token = os.environ["USC_DWAVE_TOKEN"]
if not len(token):
    print("error getting token")
    sys.exit(1)

cant_connect = True
while cant_connect:
    try:
        print('about to create sampler')
        if platform == DWavePlatform.CHIMERA: # FOR CHIMERA (AKA 2000Q)
            sampler = DWaveSampler(endpoint=url, token=token, solver="DW_2000Q_6")
        elif platform == DWavePlatform.PEGASUS: # FOR PEGASUS (AKA ADVANTAGE)
            sampler = DWaveSampler(endpoint=url, token=token, solver="Advantage_system1.1")
        print('created sampler')
        cant_connect = False
    except IOError:
        print('Network error, trying again', datetime.datetime.now())
        time.sleep(10)
        cant_connect = True
A_adj = sampler.adjacency
A = sampler.to_networkx_graph()

def total_hamiltonian(s, C_i, C_ij):
    bits = len(s)
    h = 0 - np.dot(s, C_i)
    for i in range(bits):
        h += s[i] * np.dot(s[i+1:], C_ij[i][i+1:])
    return h
    
def hamiltonian(s, C_i, C_ij, mu, sigma, reg):
    s[np.where(s > 1)] = 1.0
    s[np.where(s < -1)] = -1.0
    bits = len(s)
    h = 0
    for i in range(bits):
        h += 2*s[i]*(-sigma[i]*C_i[i])
        for j in range(bits):
            if j > i:
                h += 2*s[i]*s[j]*sigma[i]*sigma[j]*C_ij[i][j]
            h += 2*s[i]*sigma[i]*C_ij[i][j] * mu[j]
    return h


def anneal(C_i, C_ij, mu, sigma, l, strength_scale, energy_fraction, ngauges, max_excited_states):
    h = np.zeros(len(C_i))
    J = {}
    for i in range(len(C_i)):
        h_i = -2*sigma[i]*C_i[i]
        for j in range(len(C_ij[0])):
            if j > i:
                J[(i, j)] = 2*C_ij[i][j]*sigma[i]*sigma[j]
            h_i += 2*(sigma[i]*C_ij[i][j]*mu[j]) 
        h[i] = h_i

    vals = np.array(list(J.values()))
    cutoff = np.percentile(vals, AUGMENT_CUTOFF_PERCENTILE)
    to_delete = []
    for k, v in J.items():
        if v < cutoff:
            to_delete.append(k)
    for k in to_delete:
        del J[k]


    if FIXING_VARIABLES:
        bqm = BQM.from_ising(h, J)
        fixed_dict = fix_variables(bqm)
        fixed_bqm = bqm.copy()
        for i in fixed_dict.keys():
            # As of now, don't need to store the vars fixed (ret_store)
            ret_store = fixed_bqm.add_variable(i, fixed_dict[i])
        print('new length', len(fixed_bqm))
    if (not FIXING_VARIABLES) or len(fixed_bqm) > 0:
        mapping = []
        offset = 0
        for i in range(len(C_i)):
            if i in fixed_dict:
                mapping.append(None)
                offset += 1
            else:
                mapping.append(i - offset)
        
        # run gauges
        nreads = 200
        qaresults = np.zeros((ngauges*nreads, len(h)))
        for g in range(ngauges):
            embedded = False
            for attempt in range(5):
                a = np.sign(np.random.rand(len(h)) - 0.5)
                h_gauge = h*a
                J_gauge = {}
                for i in range(len(h)):
                    for j in range(len(h)):
                        if (i, j) in J:
                            J_gauge[(i, j)] = J[(i, j)]*a[i]*a[j]

                # Need to make J and A NetworkX Graphs (type)
                J_NetworkX = Graph()
                for i in range(len(h_gauge)):
                    J_NetworkX.add_node(i, weight=h_gauge[i])
                for k, v in J_gauge.items():
                    J_NetworkX.add_edge(k[0], k[1], weight=v)
                embedding = find_embedding(J_NetworkX, A)
                try:
                    th, tJ = embed_ising(h_gauge, J_gauge, embedding, A_adj)
                    embedded = True
                    break
                except ValueError:      # no embedding found
                    print('no embedding found')
                    embedded = False
                    continue
            
            if not embedded:
                continue
            
            # adjust chain strength
            for k in list(tJ.keys()):
                tJ[k] /= strength_scale 
            for k in list(th.keys()):
                th[k] /= strength_scale 

            emb_j =  tJ.copy()
            #emb_j.update(jc) -> "jc" not returned anymore bc embed func changed

            print(g)
            print("Quantum annealing")
            try_again = True
            while try_again:
                try:
                    qaresult = sampler.sample_ising(th, emb_j, num_reads = nreads, annealing_time = a_time, answer_mode='raw')
                    try_again = False
                except:
                    print('runtime or ioerror, trying again')
                    time.sleep(10)
                    try_again = True
            print("Quantum submitted") # client.py uses threading so technically annealing isn't done yet

            unembed_qaresult = unembed_sampleset(qaresult, embedding, bqm)
            # orig_sample = np.array(unembed_qaresult.record.sample)
            # unembed_qaresult.record.sample[:, :] *= a
            for i in range(len(unembed_qaresult.record.sample)):
                unembed_qaresult.record.sample[i, :] = unembed_qaresult.record.sample[i, :] * a
            qaresults[g*nreads:(g+1)*nreads] = unembed_qaresult.record.sample
        
        full_strings = np.zeros((len(qaresults), len(C_i)))
        if FIXING_VARIABLES:
            j = 0
            for i in range(len(C_i)):
                if i in fixed_dict:
                    full_strings[:, i] = 2*fixed_dict[i] - 1  
                else:
                    full_strings[:, i] = qaresults[:, j]  
                    j += 1
        else:
            full_strings = qaresults  
        
        s = full_strings 
        energies = np.zeros(len(qaresults))
        s[np.where(s > 1)] = 1.0
        s[np.where(s < -1)] = -1.0
        bits = len(s[0])
        for i in range(bits):
            energies += 2*s[:, i]*(-sigma[i]*C_i[i])
            for j in range(bits):
                if j > i:
                    energies += 2*s[:, i]*s[:, j]*sigma[i]*sigma[j]*C_ij[i][j]
                energies += 2*s[:, i]*sigma[i]*C_ij[i][j] * mu[j]
        
        unique_energies, unique_indices = np.unique(energies, return_index=True)
        ground_energy = np.amin(unique_energies)
        if ground_energy < 0:
            threshold_energy = (1 - energy_fraction) * ground_energy
        else:
            threshold_energy = (1 + energy_fraction) * ground_energy
        lowest = np.where(unique_energies < threshold_energy)
        unique_indices = unique_indices[lowest]
        if len(unique_indices) > max_excited_states:
            sorted_indices = np.argsort(energies[unique_indices])[-max_excited_states:]
            unique_indices = unique_indices[sorted_indices]
        final_answers = full_strings[unique_indices]
        print('number of selected excited states', len(final_answers))
        
        return final_answers
        
    else:
        final_answer = []
        for i in range(len(C_i)):
            if i in fixed_dict:
                final_answer.append(2*fixed_dict[i] - 1)
        final_answer = np.array(final_answer)
        return np.array([final_answer])


# Creates the data snipets using the small iteration smaples
def create_augmented_data(sig, bkg): # sig and bkg are only the portions sampled this iteration, out of the total sig and bkg
    offset = AUGMENT_OFFSET
    scale = AUGMENT_SIZE

    n_samples = len(sig) + len(bkg) # total number of sampled data points
    n_classifiers = sig.shape[1] # ???
    predictions_raw = np.concatenate((sig, bkg)) # combine the two arrays into a larger array
    predictions_raw = np.transpose(predictions_raw) # ???
    predictions = np.zeros((n_classifiers * scale, n_samples)) # make an array of zeroes at some length???
    for i in range(n_classifiers):
        for j in range(scale):
            predictions[i*scale + j] = np.sign(predictions_raw[i] + (j-scale//2)*offset) / (n_classifiers * scale)
    y = np.concatenate((np.ones(len(sig)), -np.ones(len(bkg))))
    print('predictions', predictions)
    return predictions, y

# Does something like combining the training into one place???
def ensemble(predictions, weights):
    ensemble_predictions = np.zeros(len(predictions[0]))
    
    return np.sign(np.dot(predictions.T, weights))

def rand_delete(remaining_val, num_samples):
    # Potentially want to return array of sampled indeces, left in for convenience
    # picked_indeces = np.array()
    picked_values = np.array(0)
    for i in range(int(num_samples)):
        picked_index = rng.integers(0, len(remaining_val))
        # picked_indeces = np.append(picked_index)
        picked_values = np.append(picked_values, remaining_val[picked_index])
        remaining_val = np.delete(remaining_val, picked_index)
    
    return picked_values

# Step 1: Load Background and Signal data
print('loading data')
sig = np.loadtxt('sig.csv')
bkg = np.loadtxt('bkg.csv')
# 1.1 - After loading, Calculate the percentage of bkg and sig out of the total data (combination of the two)
sig_pct = float(len(sig)) / (len(sig) + len(bkg))
bkg_pct = float(len(bkg)) / (len(sig) + len(bkg))
print('loaded data')

# Step 2: Initialize n-folds variable and num outside of the for-loop
n_folds = 10
# num = 0

#Step 3: Loop over all the different training sizes (train_size declared/defined below imports)
for train_size in train_sizes:
    num = 0
    print('training with size', train_size)
    # 3.1 - Create arrays with sizes equal to the sizes of bkg and sig
    sig_indices = np.arange(len(sig))
    bkg_indices = np.arange(len(bkg))
    
    # 3.2 - Create indexing vars with sizes equal to the sizes of the recently created "indices" arrays, and bkg and sig datas
    remaining_sig = sig_indices
    remaining_bkg = bkg_indices

    # 3.3 - generate folds(what are folds??) using 0 as the seed -> potentially use a different quantum algorithm
    #        to generate actual random numbers
    # fold_generator = np.random.RandomState(0)

    #Step 5: iterate over the number of folds
    for f in range(n_folds):
        if num >= end_num: # end_num declared/defined below imports 
            break
        print('fold', f)
        # 5.1-5.2 - returns "train" arrays by randomly sampling some of "reamining" arrays -> the amount of some 
        #        determined by the iteration of train_size * data pct. "train" arrays store the indeces that were 
        #        randomly selected to train with that iteration
        #         - update "remaining" arrays for next loop by deleting the indeces of the data sampled that iteration, 
        #        so this stores a long-term record of unused indeces (a record across all iterations)
        train_sig = rand_delete(remaining_sig, num_samples=sig_pct*train_size)
        train_bkg = rand_delete(remaining_bkg, num_samples=bkg_pct*train_size)
        
        # 5.3 - create/overwrite "test" arrays by temporarily deleting the indeces of the data sampled that iteration, 
        #        however at the next iteration test_sig will be overwritten to only have deleted the indeces used that 
        #        iteration, so this stores a short-term record of unused indeces (a record only from the current iteration)
        test_sig = np.delete(sig_indices, train_sig)
        test_bkg = np.delete(bkg_indices, train_bkg)

        # 5.4 - creates "train" and "test" vars by sending the pieces of the data arrays ("sig" and "bkg") as 
        #        determined by the indeces produced from the random samplings ("train" and "test" arrays), 
        #        so as time goes on the "train" arrays will get smaller, but the "test" arrays will stay the same size
        predictions_train, y_train = create_augmented_data(sig[train_sig], bkg[train_bkg])
        predictions_test, y_test = create_augmented_data(sig[test_sig], bkg[test_bkg])
        print('split data')
        
        # 5.5 - increment num
        if num < start_num:
            num += 1
            continue

        # 5.6 - create C_ij and C_i matrices
        n_classifiers = len(predictions_train)
        C_ij = np.zeros((n_classifiers, n_classifiers))
        C_i = np.dot(predictions_train, y_train)
        for i in range(n_classifiers):
            for j in range(n_classifiers):
                C_ij[i][j] = np.dot(predictions_train[i], predictions_train[j])

        print('created C_ij and C_i matrices')

        # 5.7 - Create/update ML vars based on matrices
        sigma = np.ones(n_classifiers)
        reg = 0.0
        l0 = reg*np.amax(np.diagonal(C_ij)*sigma*sigma - 2*sigma*C_i)
        strengths = [3.0, 1.0, 0.5, 0.2] + [0.1]*(n_iterations - 4)
        energy_fractions = [0.08, 0.04, 0.02] + [0.01]*(n_iterations - 3)
        gauges = [50, 10] + [1]*(n_iterations - 2)
        max_states = [16, 4] + [1]*(n_iterations - 2)     # cap the number of excited states accepted per iteration

        mus = [np.zeros(n_classifiers)]
        iterations = n_iterations
        for i in range(iterations):
            print('iteration', i)
            l = reg*np.amax(np.diagonal(C_ij)*sigma*sigma - 2*sigma*C_i)
            new_mus = []
            for mu in mus:
                excited_states = anneal(C_i, C_ij, mu, sigma, l, strengths[i], energy_fractions[i], gauges[i], max_states[i])
                for s in excited_states:
                    new_energy = total_hamiltonian(mu + s*sigma*zoom_factor, C_i, C_ij) / (train_size - 1)
                    flips = np.ones(len(s))
                    for a in range(len(s)):
                        temp_s = np.copy(s)
                        temp_s[a] = 0
                        old_energy = total_hamiltonian(mu + temp_s*sigma*zoom_factor, C_i, C_ij) / (train_size - 1)
                        energy_diff = new_energy - old_energy
                        if energy_diff > 0:
                            flip_prob = flip_probs[i]
                            flip = np.random.choice([1, flip_state], size=1, p=[1-flip_prob, flip_prob])[0]
                            flips[a] = flip
                        else:
                            flip_prob = flip_others_probs[i]
                            flip = np.random.choice([1, flip_state], size=1, p=[1-flip_prob, flip_prob])[0]
                            flips[a] = flip
                    flipped_s = s * flips
                    new_mus.append(mu + flipped_s*sigma*zoom_factor)
            sigma *= zoom_factor
            mus = new_mus
            
            mus_filename = 'mus%05d_fold%d_iter%d-%s.npy' % (train_size, f, i, timestamp)
            mus_destdir = os.path.join(script_path, 'mus')
            mus_filepath = (os.path.join(mus_destdir, mus_filename))
            if not os.path.exists(mus_destdir):
                os.makedirs(mus_destdir)
            np.save(mus_filepath, np.array(mus))
        accuracy_dict = {}
        test_point = {
            "train_size": train_size,
            "train_accuracy": [],
            "test_accuracy": [],
        }
        for mu in mus:
            test_point["train_accuracy"].append(accuracy_score(y_train, ensemble(predictions_train, mu)))
            test_point["test_accuracy"].append(accuracy_score(y_test, ensemble(predictions_test, mu)))
        test_results['results'].append(test_point)
        print('final average accuracy on train set', np.mean(np.array(test_point["train_accuracy"])))
        print('inal average accuracy on test set', np.mean(np.array(test_point["test_accuracy"])))
        num += 1
filename = 'accuracy_results-%s.json' % timestamp
destdir = os.path.join(script_path, 'qamlz_runs')
filepath = os.path.join(destdir, filename)
if not os.path.exists(destdir):
    os.makedirs(destdir)
json.dump(test_results, open(filepath, 'w'), indent=4)
