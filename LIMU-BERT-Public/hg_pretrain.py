#!/usr/bin/env python
import argparse
import sys
import pandas as pd
import ast
import numpy as np
import torch
import torch.nn as nn
import copy
import os
from torch.utils.data import Dataset, TensorDataset, DataLoader
from datetime import datetime
import models, train, tracking, plot
from config import MaskConfig, TrainConfig, PretrainModelConfig
from models import LIMUBertModel4Pretrain, LIMUBertMultiMAEModel4Pretrain, LIMUBertAEModel4Pretrain
from utils import set_seeds, get_device, LIBERTMultiDataset4Pretrain,LIBERTGazeDataset4Pretrain, handle_argv, load_pretrain_data_config, prepare_classifier_dataset, \
    prepare_pretrain_dataset, Preprocess4Normalization,  Preprocess4Mask
import mlflow
from statistic import compute_dtw_metric, compute_levenschtein_distance, compute_euclidean_distance
from hyperparameter_opt import HyperparameterOptimization
import itertools


def preprocess_one_csv(path, seq_len, downsample_ratio):
    df = pd.read_csv(path)
    df = df.dropna()
    df['RightGazeDirection'] = df['RightGazeDirection'].apply(lambda x: ast.literal_eval(x))
    df['Unit_Vector'] = df['Unit_Vector'].apply(lambda x: ast.literal_eval(x))
    gaze = df["RightGazeDirection"][::downsample_ratio].values.tolist()
    head = df["Unit_Vector"][::downsample_ratio].values.tolist()
    if not len(gaze) < seq_len:
        gaze = np.array(gaze[:(len(gaze)//seq_len * seq_len)])
        gaze = np.array(np.split(gaze, len(gaze)//seq_len)) 
        head = np.array(head[:(len(head)//seq_len * seq_len)])
        head = np.array(np.split(head, len(head)//seq_len))
        return gaze, head


def preprocess_hgbd_dataset(args):
    dataset_cfg = args.dataset_cfg
    gaze = np.empty((0, dataset_cfg.seq_len, dataset_cfg.dimension))
    head = np.empty((0, dataset_cfg.seq_len, dataset_cfg.dimension))
    for per in os.listdir("../Data/Version2"):
        print(per)
        try:
            csvs = os.listdir("../Data/Version2/" + per)
            for f in range(len(csvs)):
                temp1, temp2 = preprocess_one_csv("../Data/Version2/" + per + "/" + csvs[f], dataset_cfg.seq_len, dataset_cfg.downsample_ratio)
                if temp1 is not None and temp2 is not None:
                    gaze = np.concatenate((gaze, temp1), axis=0)
                    head = np.concatenate((head, temp2), axis=0)
        except:
            pass
    assert head.shape == gaze.shape
    np.save("../LIMU-BERT-Public/dataset/hgbd/data_2.npy", gaze)
    np.save("../LIMU-BERT-Public/dataset/hgbd/label_2.npy", head)
    # np.save("../LIMU-BERT-Public/dataset/hgbd/label_2.npy", np.ones(gaze.shape))


def main(args, training_rate, tracker, combination):
    # preprocess_hgbd_dataset(args)
    lr, batch_size, mask_ratio = combination

    mlflow.log_params({
        "learning_rate": lr,
        "batch_size": batch_size,
        "mask_ratio": mask_ratio
    })

    gdata, hdata, train_cfg, model_cfg, mask_cfg, dataset_cfg = load_pretrain_data_config(args)
    train_cfg = TrainConfig(seed=18, 
                        batch_size=batch_size, 
                        lr=lr, 
                        n_epochs=5, 
                        warmup=0.1, 
                        save_steps=1000, 
                        total_steps=200000, 
                        lambda1=0,
                        lambda2=0) 
    #Setting mask_cfg from hyperparameters:
    mask_cfg = MaskConfig(mask_ratio=mask_ratio, 
                      mask_alpha=mask_cfg.mask_alpha, 
                      max_gram=mask_cfg.max_gram, 
                      mask_prob=mask_cfg.mask_prob, 
                      replace_prob=mask_cfg.replace_prob)


    #import pdb; pdb.set_trace()
    #pipeline = [Preprocess4Normalization(model_cfg.feature_num), Preprocess4Mask(mask_cfg)]
    pipeline = [Preprocess4Mask(mask_cfg)]
    gdata_train, hdata_train, gdata_val, hdata_val, gdata_test, hdata_test = prepare_pretrain_dataset(gdata, hdata, training_rate, seed=train_cfg.seed)

    if args.model_type == 'gaze':
        dataset_pretrain = LIBERTGazeDataset4Pretrain
        model = LIMUBertAEModel4Pretrain(model_cfg)
        data_set_train = dataset_pretrain(gdata_train, pipeline=pipeline)
        data_set_val = dataset_pretrain(gdata_val, pipeline=pipeline)
        data_set_test = dataset_pretrain(gdata_test, pipeline=pipeline, istestset=True)
    else:
        dataset_pretrain = LIBERTMultiDataset4Pretrain
        if args.model_type == 'gaze_mm':
            model = LIMUBertMultiMAEModel4Pretrain(model_cfg,recon_head=False)
        elif args.model_type == 'head_gaze_mm':
            model = LIMUBertMultiMAEModel4Pretrain(model_cfg,recon_head=True)
        data_set_train = dataset_pretrain(gdata_train, hdata_train, pipeline=pipeline)
        data_set_val = dataset_pretrain(gdata_val, hdata_val, pipeline=pipeline)
        data_set_test = dataset_pretrain(gdata_test, hdata_test, pipeline=pipeline)
        
    data_loader_train = DataLoader(data_set_train, shuffle=True, batch_size=train_cfg.batch_size)
    data_loader_val = DataLoader(data_set_val, shuffle=True, batch_size=train_cfg.batch_size)
    data_loader_test = DataLoader(data_set_test, shuffle=False, batch_size=train_cfg.batch_size)
    
    criterion = nn.MSELoss(reduction='none')
    optimizer = torch.optim.Adam(params=model.parameters(), lr=lr)
    device = get_device(args.gpu)
    trainer = train.Trainer(train_cfg, model, optimizer, args.save_path, device)

    def conv_spherical(seq):
        if not isinstance(seq, torch.Tensor):
            seq = torch.Tensor(seq)
        
        x = seq[..., 0]
        y = seq[..., 1]
        z = seq[..., 2]
        theta = torch.atan2(y, x)
        phi = torch.acos(z)
        spherical_coords = torch.stack((theta, phi), -1)
        return spherical_coords

    def func_loss(model, batch):
        if args.model_type == 'gaze':
            gmask_seqs, gmasked_pos, gseqs = batch
            gseq_recon = model(gmask_seqs, gmasked_pos) 
            gloss_lm = criterion(gseq_recon, gseqs) 
            loss_lm = gloss_lm
        else:    
            gmask_seqs, gmasked_pos, gseqs, hmask_seqs, hmasked_pos, hseqs = batch
            if args.model_type == 'gaze_mm':
                gseq_recon = model(gmask_seqs, hmask_seqs, gmasked_pos) 
                gloss_lm = criterion(gseq_recon, gseqs)
                loss_lm = gloss_lm
            elif args.model_type == 'head_gaze_mm':
                gseq_recon, hseq_recon = model(gmask_seqs, hmask_seqs, gmasked_pos)
                gloss_lm = criterion(gseq_recon, gseqs)
                hloss_lm = criterion(hseq_recon, hseqs)
                loss_lm = gloss_lm + hloss_lm
        return loss_lm

    def func_forward(model, batch):
        if args.model_type == 'gaze_mm':
            gmask_seqs, gmasked_pos, gseqs, hmask_seqs, hmasked_pos, hseqs = batch
            gseq_recon = model(gmask_seqs, hmask_seqs, gmasked_pos)
            return gseq_recon, gseqs
        elif args.model_type == 'head_gaze_mm':
            gmask_seqs, gmasked_pos, gseqs, hmask_seqs, hmasked_pos, hseqs = batch
            gseq_recon, hseq_recon = model(gmask_seqs, hmask_seqs, gmasked_pos)
            return torch.concat((gseq_recon,hseq_recon), dim = 1), torch.concat((gseqs,hseqs), dim = 1)
        else:
            gmask_seqs, gmasked_pos, gseqs = batch
            gseq_recon = model(gmask_seqs, gmasked_pos)
            return gseq_recon, gseqs
        
    def func_evaluate(seqs, predict_seqs):
        if args.model_type == 'gaze_mm':
            gloss_lm = criterion(predict_seqs, seqs)
            return gloss_lm.mean().cpu().numpy()
        elif args.model_type == 'head_gaze_mm':
            hgloss_lm = criterion(predict_seqs, seqs)
            return hgloss_lm.mean().cpu().numpy()
        else:
            gloss_lm = criterion(predict_seqs, seqs)
            return gloss_lm.mean().cpu().numpy()

    tracker.log_parameters(train_cfg, model_cfg, mask_cfg, dataset_cfg)
    
    if hasattr(args, 'pretrain_model'):
        val_loss, test_loss, train_loss = trainer.pretrain(func_loss, func_forward, func_evaluate, data_loader_train, data_loader_val, data_loader_test
                    , model_file=args.pretrain_model, tracker=tracker)
    else:
        val_loss, test_loss, train_loss = trainer.pretrain(func_loss, func_forward, func_evaluate, data_loader_train, data_loader_val, data_loader_test, model_file=None, tracker=tracker)

    tracker.log_model(model, "models")
    tracker.log_metrics("Test Loss", test_loss)

    estimate_test, actual_test = trainer.run(func_forward, None, data_loader_test, return_labels=True)

    estimate_test_sph, actual_test_sph = conv_spherical(estimate_test), conv_spherical(actual_test)

    return actual_test_sph, estimate_test_sph


if __name__ == "__main__":
    mode = "base"
    args = handle_argv('pretrain_' + mode, 'pretrain.json', mode)
    training_rate = 0.8

    hyperParamOptimization = HyperparameterOptimization('./config/hparams.yaml')
    params = hyperParamOptimization.get_params()

    tracker = tracking.MLFlowTracker("Gaze_MM_Tuning")
    tracker.set_experiment()

    seed = 2024
    hyperparameter_combinations = itertools.product(params.T_LR, params.T_BATCHSIZE, params.D_MASKRATIO)
    for i,combination in enumerate(hyperparameter_combinations):
        with mlflow.start_run(description="A MultiModal Transformer Gaze Reconstruction"):
            actual_test, estimate_test = main(args, training_rate, tracker, combination)
            gaze_actual_test, gaze_estimate_test, head_actual_test, head_estimate_test  = [], [], [], []
            if args.model_type == 'head_gaze_mm':
                seq_len = estimate_test.shape[1]
                gaze_len = seq_len // 2
                gaze_estimate_test, head_estimate_test = estimate_test[:,:gaze_len,:], estimate_test[:,gaze_len:,:]
                gaze_actual_test, head_actual_test = actual_test[:,:gaze_len,:], actual_test[:,gaze_len:,:]
                tracker.log_metrics("Test Dynamic Time Warping Head", compute_dtw_metric(head_estimate_test, head_actual_test))
                tracker.log_metrics("Test Euclidean Distance Head", compute_euclidean_distance(head_estimate_test, head_actual_test))
                tracker.log_metrics("Test Euclidean Distance Gaze", compute_euclidean_distance(gaze_estimate_test, gaze_actual_test))
                tracker.log_metrics("Test Dynamic Time Warping Gaze", compute_dtw_metric(gaze_estimate_test, gaze_actual_test))
                tracker.log_metrics("Test Euclidean Distance", 
                    compute_euclidean_distance(gaze_estimate_test, gaze_actual_test)+compute_euclidean_distance(head_estimate_test, head_actual_test))
                tracker.log_metrics("Test Dynamic Time Warping", 
                    compute_dtw_metric(gaze_estimate_test, gaze_actual_test)+compute_dtw_metric(head_estimate_test, head_actual_test))

                datestr = datetime.now().strftime("%d.%m.%Y.%H.%M")
                plot.plot_sequences_3d(gaze_estimate_test, gaze_actual_test, "3D_Spherical_Coord_Gaze_", datestr, seed=seed)
                tracker.log_artifact(os.path.join(os.getcwd(), "results", f"3D_Spherical_Coord_Gaze_{datestr}.png"))
                plot.plot_sequences_3d(head_estimate_test, head_actual_test, "3D_Spherical_Coord_Head_", datestr, seed=seed)
                tracker.log_artifact(os.path.join(os.getcwd(), "results", f"3D_Spherical_Coord_Head_{datestr}.png"))
                plot.plot_sequences_2d(gaze_estimate_test, gaze_actual_test, "2D_Spherical_Coord_Gaze_", datestr, seed=seed)
                tracker.log_artifact(os.path.join(os.getcwd(), "results", f"2D_Spherical_Coord_Gaze_{datestr}.png"))
                plot.plot_sequences_2d(head_estimate_test, head_actual_test, "2D_Spherical_Coord_Head_", datestr, seed=seed)
                tracker.log_artifact(os.path.join(os.getcwd(), "results", f"2D_Spherical_Coord_Head_{datestr}.png"))
                tracker.log_artifact(args.save_path+'.pt')
            else:
                tracker.log_metrics("Test Euclidean Distance", compute_euclidean_distance(estimate_test, actual_test))
                tracker.log_metrics("Test Dynamic Time Warping", compute_dtw_metric(estimate_test, actual_test))
                datestr = datetime.now().strftime("%d.%m.%Y.%H.%M")
                plot.plot_sequences_3d(estimate_test, actual_test, "3D_Spherical_Coord_Gaze_", datestr, seed=seed)
                tracker.log_artifact(os.path.join(os.getcwd(), "results", f"3D_Spherical_Coord_Gaze_{datestr}.png"))
                plot.plot_sequences_2d(estimate_test, actual_test, "2D_Spherical_Coord_Gaze_", datestr, seed=seed)
                tracker.log_artifact(os.path.join(os.getcwd(), "results", f"2D_Spherical_Coord_Gaze_{datestr}.png"))
                tracker.log_artifact(args.save_path+'.pt')