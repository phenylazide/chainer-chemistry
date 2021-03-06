#!/usr/bin/env python
from __future__ import print_function

import argparse
import numpy
import os
import types

import pickle

import chainer
from chainer import iterators
from chainer import optimizers
from chainer import training

from chainer.training import extensions as E

from chainer_chemistry.dataset.converters import converter_method_dict
from chainer_chemistry.dataset.preprocessors import preprocess_method_dict, wle
from chainer_chemistry import datasets as D
from chainer_chemistry.datasets.molnet.molnet_config import molnet_default_config  # NOQA
from chainer_chemistry.datasets import NumpyTupleDataset
from chainer_chemistry.dataset.splitters.deepchem_scaffold_splitter import DeepChemScaffoldSplitter  # NOQA
from chainer_chemistry.links import StandardScaler
from chainer_chemistry.models.prediction import Classifier
from chainer_chemistry.models.prediction import Regressor
from chainer_chemistry.models.prediction import set_up_predictor
from chainer_chemistry.training.extensions.auto_print_report import AutoPrintReport  # NOQA
from chainer_chemistry.utils import save_json
from chainer_chemistry.models.cwle.cwle_graph_conv_model import MAX_WLE_NUM


def dict_for_wles():
    wle_keys = ['nfp_wle', 'ggnn_wle',  'relgat_wle', 'relgcn_wle', 'rsgcn_wle', 'gin_wle',
                   'nfp_cwle', 'ggnn_cwle',  'relgat_cwle', 'relgcn_cwle', 'rsgcn_cwle', 'gin_cwle',
                   'nfp_gwle', 'ggnn_gwle',  'relgat_gwle', 'relgcn_gwle', 'rsgcn_gwle', 'gin_gwle']

    from chainer_chemistry.dataset.converters.concat_mols import concat_mols
    from chainer_chemistry.dataset.preprocessors.nfp_preprocessor import NFPPreprocessor
    from chainer_chemistry.dataset.preprocessors.ggnn_preprocessor import GGNNPreprocessor
    from chainer_chemistry.dataset.preprocessors.gin_preprocessor import GINPreprocessor
    from chainer_chemistry.dataset.preprocessors.relgat_preprocessor import RelGATPreprocessor
    from chainer_chemistry.dataset.preprocessors.relgcn_preprocessor import RelGCNPreprocessor
    from chainer_chemistry.dataset.preprocessors.rsgcn_preprocessor import RSGCNPreprocessor

    for key in wle_keys:
        converter_method_dict[key] = concat_mols

        if key.startswith('nfp'):
            preprocess_method_dict[key] = NFPPreprocessor
        elif key.startswith('ggnn'):
            preprocess_method_dict[key] = GGNNPreprocessor
        elif key.startswith('gin'):
            preprocess_method_dict[key] = GINPreprocessor
        elif key.startswith('relgcn'):
            preprocess_method_dict[key] = RelGCNPreprocessor
        elif key.startswith('rsgcn'):
            preprocess_method_dict[key] = RSGCNPreprocessor
        elif key.startswith('relgat'):
            preprocess_method_dict[key] = RelGATPreprocessor
        else:
            assert key in wle_keys # should be die
dict_for_wles()

def parse_arguments():
    # Lists of supported preprocessing methods/models and datasets.
    method_list = ['nfp', 'ggnn', 'schnet', 'weavenet', 'rsgcn', 'relgcn',
                   'relgat', 'gin', 'gnnfilm',
                   'nfp_gwm', 'ggnn_gwm', 'rsgcn_gwm', 'gin_gwm',
                   'nfp_wle', 'ggnn_wle',  'relgat_wle', 'relgcn_wle', 'rsgcn_wle', 'gin_wle',
                   'nfp_cwle', 'ggnn_cwle',  'relgat_cwle', 'relgcn_cwle', 'rsgcn_cwle', 'gin_cwle',
                   'nfp_gwle', 'ggnn_gwle',  'relgat_gwle', 'relgcn_gwle', 'rsgcn_gwle', 'gin_gwle']
    dataset_names = list(molnet_default_config.keys())
    scale_list = ['standardize', 'none']

    parser = argparse.ArgumentParser(description='molnet example')
    parser.add_argument('--method', '-m', type=str, choices=method_list,
                        help='method name', default='nfp')
    parser.add_argument('--label', '-l', type=str, default='',
                        help='target label for regression; empty string means '
                        'predicting all properties at once')
    parser.add_argument('--conv-layers', '-c', type=int, default=4,
                        help='number of convolution layers')
    parser.add_argument('--batchsize', '-b', type=int, default=32,
                        help='batch size')
    parser.add_argument(
        '--device', type=str, default='-1',
        help='Device specifier. Either ChainerX device specifier or an '
             'integer. If non-negative integer, CuPy arrays with specified '
             'device id are used. If negative integer, NumPy arrays are used')
    parser.add_argument('--out', '-o', type=str, default='result',
                        help='path to save the computed model to')
    parser.add_argument('--epoch', '-e', type=int, default=20,
                        help='number of epochs')
    parser.add_argument('--unit-num', '-u', type=int, default=16,
                        help='number of units in one layer of the model')
    parser.add_argument('--dataset', '-d', type=str, choices=dataset_names,
                        default='bbbp',
                        help='name of the dataset that training is run on')
    parser.add_argument('--protocol', type=int, default=2,
                        help='pickle protocol version')
    parser.add_argument('--num-data', type=int, default=-1,
                        help='amount of data to be parsed; -1 indicates '
                        'parsing all data.')
    parser.add_argument('--scale', type=str, choices=scale_list,
                        help='label scaling method', default='standardize')
    parser.add_argument('--adam-alpha', type=float, help='alpha of adam', default=0.001)

    # WLE options
    parser.add_argument('--cutoff-wle', type=int, default=0, help="set more than zero to cut-off WL expanded labels")
    parser.add_argument('--hop-num', '-k', type=int, default=1, help="The number of iterations of WLs")

    return parser.parse_args()


def dataset_part_filename(dataset_part, num_data):
    """Returns the filename corresponding to a train/valid/test parts of a
    dataset, based on the amount of data samples that need to be parsed.
    Args:
        dataset_part: String containing any of the following 'train', 'valid'
                      or 'test'.
        num_data: Amount of data samples to be parsed from the dataset.
    """
    if num_data >= 0:
        return '{}_data_{}.npz'.format(dataset_part, str(num_data))
    return '{}_data.npz'.format(dataset_part)


def download_entire_dataset(dataset_name, num_data, labels, method, cache_dir, apply_wle_flag=False, cutoff_wle=0, apply_cwle_flag=False, apply_gwle_flag=False, n_hop=1):
    """Downloads the train/valid/test parts of a dataset and stores them in the
    cache directory.
    Args:
        dataset_name: Dataset to be downloaded.
        num_data: Amount of data samples to be parsed from the dataset.
        labels: Target labels for regression.
        method: Method name. See `parse_arguments`.
        cache_dir: Directory to store the dataset to.
        apply_wle_flag: boolean, set True if you apply the naive WL embeddding
        cutoff_wle: int set more than zero to cut off WEEs
        apply_cwle_flag: boolean, set True if you apply Concatenating WLE (CWLE)
        apply_gwle_flag: boolean, set True if you apply Gated-sum WLE (GWLE)
    """

    print('Downloading {}...'.format(dataset_name))
    preprocessor = preprocess_method_dict[method]()

    # Select the first `num_data` samples from the dataset.
    target_index = numpy.arange(num_data) if num_data >= 0 else None

    # To force DeepChem scaffold split
    dc_scaffold_splitter = DeepChemScaffoldSplitter()
    dataset_parts = D.molnet.get_molnet_dataset(dataset_name, preprocessor,
                                                labels=labels,
                                                split=dc_scaffold_splitter,
                                                target_index=target_index)

    dataset_parts = dataset_parts['dataset']

    # Cache the downloaded dataset.
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)

    # apply Neighboring Label Expansion
    if apply_wle_flag:
        dataset_parts_expand, labels_expanded, labels_frequency = wle.apply_wle_for_datasets(dataset_parts, cutoff_wle, n_hop)
        dataset_parts = dataset_parts_expand
        num_expanded_symbols = len(labels_expanded)
        print("WLE Expanded Labels Applied to datasets: vocab=", num_expanded_symbols)
        print(labels_expanded)

        # save in text
        file_name = "WLE_labels.dat"
        path = os.path.join(cache_dir, file_name)
        with open(path, "w") as fout:
            for label in labels_expanded:
                fout.write(label + " " + str(labels_frequency[label]) + "\n")

        # save binaries
        file_name = "WLE_labels.pkl"
        outfile = cache_dir + "/" + file_name
        with open(outfile, "wb") as fout:
            pickle.dump( (labels_expanded, labels_frequency), fout)

    elif apply_cwle_flag:
        dataset_parts_expand, labels_expanded, labels_frequency = wle.apply_cwle_for_datasets(dataset_parts, n_hop)
        dataset_parts = dataset_parts_expand
        num_expanded_symbols = len(labels_expanded)
        print("Concatenating WLE Expanded Labels Applied to datasets: vocab=", num_expanded_symbols)
        print(labels_expanded)

        # save in text
        file_name = "CWLE_labels.dat"
        path = os.path.join(cache_dir, file_name)
        with open(path, "w") as fout:
            for label in labels_expanded:
                fout.write(label + " " + str(labels_frequency[label]) + "\n")

        # save binaries
        file_name = "CWLE_labels.pkl"
        outfile = cache_dir + "/" + file_name
        with open(outfile, "wb") as fout:
            pickle.dump( (labels_expanded, labels_frequency), fout)

    elif apply_gwle_flag:
        dataset_parts_expand, labels_expanded, labels_frequency = wle.apply_cwle_for_datasets(dataset_parts, n_hop)
        dataset_parts = dataset_parts_expand
        num_expanded_symbols = len(labels_expanded)
        print("Gated-sum WLE Expanded Labels Applied to datasets: vocab=", num_expanded_symbols)
        print(labels_expanded)

        # save in text
        file_name = "GWLE_labels.dat"
        path = os.path.join(cache_dir, file_name)
        with open(path, "w") as fout:
            for label in labels_expanded:
                fout.write(label + " " + str(labels_frequency[label]) + "\n")

        # save binaries
        file_name = "GWLE_labels.pkl"
        outfile = cache_dir + "/" + file_name
        with open(outfile, "wb") as fout:
            pickle.dump( (labels_expanded, labels_frequency), fout)


    else:
        labels_expanded = []

    # ToDO: scaler should be placed here
    # ToDo: fit the scaler
    # ToDo: transform dataset_parts[0-2]

    for i, part in enumerate(['train', 'valid', 'test']):
        filename = dataset_part_filename(part, num_data)
        path = os.path.join(cache_dir, filename)
        if False:
            print(type(dataset_parts[i]))
            print(type(dataset_parts[i][0]))
            print(type(dataset_parts[i][0][0]))
            print(type(dataset_parts[i][0][1]))
            print(type(dataset_parts[i][0][2]))
            print(dataset_parts[i][0][0].shape)
            print(dataset_parts[i][0][1].shape)
            print(dataset_parts[i][0][2].shape)
            print(dataset_parts[i][0][0].dtype)
            print(dataset_parts[i][0][1].dtype)
            print(dataset_parts[i][0][2].dtype)
        NumpyTupleDataset.save(path, dataset_parts[i])

    return dataset_parts


def fit_scaler(datasets):
    """Standardizes (scales) the dataset labels.
    Args:
        datasets: Tuple containing the datasets.
    Returns:
        Datasets with standardized labels and the scaler object.
    """
    scaler = StandardScaler()

    # Collect all labels in order to apply scaling over the entire dataset.
    labels = None
    offsets = []
    for dataset in datasets:
        if labels is None:
            labels = dataset.get_datasets()[-1]
        else:
            labels = numpy.vstack([labels, dataset.get_datasets()[-1]])
        offsets.append(len(labels))

    scaler.fit(labels)

    return scaler


def main():
    args = parse_arguments()
    print(args)

    # Set up some useful variables that will be used later on.
    dataset_name = args.dataset
    method = args.method
    num_data = args.num_data
    n_unit = args.unit_num
    conv_layers = args.conv_layers
    adam_alpha = args.adam_alpha

    cutoff_wle = args.cutoff_wle
    n_hop = args.hop_num

    apply_wle_flag = method in ['nfp_wle', 'ggnn_wle',  'relgat_wle', 'relgcn_wle', 'rsgcn_wle', 'gin_wle']
    apply_cwle_flag = method in ['nfp_cwle', 'ggnn_cwle',  'relgat_cwle', 'relgcn_cwle', 'rsgcn_cwle', 'gin_cwle']
    apply_gwle_flag = method in ['nfp_gwle', 'ggnn_gwle',  'relgat_gwle', 'relgcn_gwle', 'rsgcn_gwle', 'gin_gwle']

    task_type = molnet_default_config[dataset_name]['task_type']
    model_filename = {'classification': 'classifier.pkl',
                      'regression': 'regressor.pkl'}

    print('Using dataset: {}...'.format(dataset_name))

    # Set up some useful variables that will be used later on.
    if args.label:
        labels = args.label
        cache_dir = os.path.join('input', '{}_{}_{}'.format(dataset_name,
                                                            method, labels))
        class_num = len(labels) if isinstance(labels, list) else 1
    else:
        labels = None
        cache_dir = os.path.join('input', '{}_{}_all'.format(dataset_name,
                                                             method))
        class_num = len(molnet_default_config[args.dataset]['tasks'])

    # Load the train and validation parts of the dataset.
    filenames = [dataset_part_filename(p, num_data)
                 for p in ['train', 'valid', 'test']]


    # ToDo: We need to incoporeat scaler into download_entire_dataset, instead of predictors. 
    paths = [os.path.join(cache_dir, f) for f in filenames]
    if all([os.path.exists(path) for path in paths]):
        dataset_parts = []
        for path in paths:
            print('Loading cached dataset from {}.'.format(path))
            dataset_parts.append(NumpyTupleDataset.load(path))
    else:
        dataset_parts = download_entire_dataset(dataset_name, num_data, labels,
                                                method, cache_dir,
                                                apply_wle_flag, cutoff_wle, apply_cwle_flag, apply_gwle_flag, n_hop)
    train, valid = dataset_parts[0], dataset_parts[1]

    # ToDo: scaler must be incorporated into download_entire_datasets. not here
    # Scale the label values, if necessary.
    scaler = None
    if args.scale == 'standardize':
        if task_type == 'regression':
            print('Applying standard scaling to the labels.')
            scaler = fit_scaler(dataset_parts)
        else:
            print('Label scaling is not available for classification tasks.')
    else:
        print('No label scaling was selected.')

    # ToDo: set label_scaler always None
    # Set up the predictor.

    if apply_wle_flag:
        # find the num_atoms
        max_symbol_index = wle.findmaxidx(dataset_parts)
        print("number of expanded symbols (WLE) = ", max_symbol_index)
        predictor = set_up_predictor(
            method, n_unit, conv_layers, class_num,
            label_scaler=scaler, n_atom_types=max_symbol_index)
    elif apply_cwle_flag or apply_gwle_flag:
        n_wle_types = wle.findmaxidx(
            dataset_parts, 'wle_label')
        # Kenta Oono (oono@preferred.jp)
        # In the previous implementation, we use MAX_WLE_NUM
        # as the dimension of one-hot vectors for WLE labels
        # when the model is CWLE or WLNE and hop_num k = 1.
        # When k >= 2, # of wle labels can be larger than MAX_WLE_NUM,
        # which causes an error.
        # Therefore, we have increased the dimension of vectors.
        # To align with the previous experiments,
        # we change n_wle_types only if it exceeds MAX_WLE_NUM.
        n_wle_types = max(n_wle_types, MAX_WLE_NUM)
        print("number of expanded symbols (CWLE/GWLE) = ", n_wle_types)
        predictor = set_up_predictor(
            method, n_unit, conv_layers, class_num,
            label_scaler=scaler, n_wle_types=n_wle_types)
    else:
        predictor = set_up_predictor(
            method, n_unit, conv_layers, class_num,
            label_scaler=scaler)

    # Set up the iterators.
    train_iter = iterators.SerialIterator(train, args.batchsize)
    valid_iter = iterators.SerialIterator(valid, args.batchsize, repeat=False,
                                          shuffle=False)

    # Load metrics for the current dataset.
    metrics = molnet_default_config[dataset_name]['metrics']
    metrics_fun = {k: v for k, v in metrics.items()
                   if isinstance(v, types.FunctionType)}
    loss_fun = molnet_default_config[dataset_name]['loss']

    device = chainer.get_device(args.device)
    if task_type == 'regression':
        model = Regressor(predictor, lossfun=loss_fun,
                          metrics_fun=metrics_fun, device=device)
    elif task_type == 'classification':
        model = Classifier(predictor, lossfun=loss_fun,
                           metrics_fun=metrics_fun, device=device)
    else:
        raise ValueError('Invalid task type ({}) encountered when processing '
                         'dataset ({}).'.format(task_type, dataset_name))

    # Set up the optimizer.
    optimizer = optimizers.Adam(alpha=adam_alpha)
    optimizer.setup(model)

    # Save model-related output to this directory.
    if not os.path.exists(args.out):
        os.makedirs(args.out)
    save_json(os.path.join(args.out, 'args.json'), vars(args))
    model_dir = os.path.join(args.out, os.path.basename(cache_dir))
    if not os.path.exists(model_dir):
        os.makedirs(model_dir)

    # Set up the updater.
    converter = converter_method_dict[method]
    updater = training.StandardUpdater(train_iter, optimizer, device=device,
                                       converter=converter)

    # Set up the trainer.
    trainer = training.Trainer(updater, (args.epoch, 'epoch'), out=model_dir)
    trainer.extend(E.Evaluator(valid_iter, model, device=device,
                               converter=converter))
    trainer.extend(E.snapshot(), trigger=(args.epoch, 'epoch'))
    trainer.extend(E.LogReport())

    # TODO: consider go/no-go of the following block
    # # (i) more reporting for val/evalutaion
    # # (ii) best validation score snapshot
    # if task_type == 'regression':
    #     metric_name_list = list(metrics.keys())
    #     if 'RMSE' in metric_name_list:
    #         trainer.extend(E.snapshot_object(model, "best_val_" + model_filename[task_type]),
    #                        trigger=training.triggers.MinValueTrigger('validation/main/RMSE'))
    #     elif 'MAE' in metric_name_list:
    #         trainer.extend(E.snapshot_object(model, "best_val_" + model_filename[task_type]),
    #                        trigger=training.triggers.MinValueTrigger('validation/main/MAE'))
    #     else:
    #         print("[WARNING] No validation metric defined?")
    #
    # elif task_type == 'classification':
    #     train_eval_iter = iterators.SerialIterator(
    #         train, args.batchsize, repeat=False, shuffle=False)
    #     trainer.extend(ROCAUCEvaluator(
    #         train_eval_iter, predictor, eval_func=predictor,
    #         device=args.gpu, converter=concat_mols, name='train',
    #         pos_labels=1, ignore_labels=-1, raise_value_error=False))
    #     # extension name='validation' is already used by `Evaluator`,
    #     # instead extension name `val` is used.
    #     trainer.extend(ROCAUCEvaluator(
    #         valid_iter, predictor, eval_func=predictor,
    #         device=args.gpu, converter=concat_mols, name='val',
    #         pos_labels=1, ignore_labels=-1, raise_value_error=False))
    #
    #     trainer.extend(E.snapshot_object(
    #         model, "best_val_" + model_filename[task_type]),
    #         trigger=training.triggers.MaxValueTrigger('val/main/roc_auc'))
    # else:
    #     raise NotImplementedError(
    #         'Not implemented task_type = {}'.format(task_type))

    trainer.extend(AutoPrintReport())
    trainer.extend(E.ProgressBar())
    trainer.run()

    # Save the model's parameters.
    model_path = os.path.join(model_dir,  model_filename[task_type])
    print('Saving the trained model to {}...'.format(model_path))
    model.save_pickle(model_path, protocol=args.protocol)

    # dump the parameter, if CWLE
    #if apply_cwle_flag:
    #    cwle = predictor.graph_conv
    #    #print(cwle)
    #    concatW = cwle.linear_for_concat_wle.W.data
    #    #print(type(concatW))
    #
    #    # dump the raw W
    #    out_prefix = args.out + "/" + method + "_" + dataset_name +"_learnedW"
    #    with open(out_prefix + ".dat", 'w') as fout:
    #        import csv
    #        writer = csv.writer(fout, lineterminator="\n")
    #        writer.writerows(concatW)
    #    # end with
    #
    #    import matplotlib
    #    matplotlib.use('Agg')
    #    import matplotlib.pyplot as plt
    #    # visualize
    #    fig1, ax1 = plt.subplots()
    #    plt.imshow(concatW, cmap="jet")
    #    plt.colorbar(ax=ax1)
    #
    #    plt.title('Learned W on ' + dataset_name + ' + ' + method)
    #    plt.savefig(out_prefix + ".png")
    #    plt.savefig(out_prefix + ".pdf")
    #
    #    # visualize the absolute value
    #    fig2, ax2 = plt.subplots()
    #    plt.imshow(numpy.abs(concatW), cmap="jet")
    #    plt.colorbar(ax=ax2)
    #
    #    plt.title('Learned abs(W) on ' + dataset_name + ' + ' + method)
    #    plt.savefig(out_prefix + "_abs.png")
    #    plt.savefig(out_prefix + "_abs.pdf")

if __name__ == '__main__':
    main()
