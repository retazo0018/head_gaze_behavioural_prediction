# !/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 14/1/2021
# @Author  : Huatao
# @Email   : 735820057@qq.com
# @File    : statistic.py
# @Description :
import numpy as np
from sklearn import metrics
from sklearn.metrics import f1_score
from fastdtw import fastdtw
import Levenshtein
from plot import plot_matrix
from scipy.spatial.distance import euclidean


def stat_acc_f1(label, results_estimated):
    # label = np.concatenate(label, 0)
    # results_estimated = np.concatenate(results_estimated, 0)
    label_estimated = np.argmax(results_estimated, 1)
    f1 = f1_score(label, label_estimated, average='macro')
    acc = np.sum(label == label_estimated) / label.size
    return acc, f1


def stat_acc_f1_dual(label, results_estimated):
    label = np.concatenate(label, 0)
    results_estimated = np.concatenate([t[1] for t in results_estimated], 0)
    label_estimated = np.argmax(results_estimated, 1)
    f1 = f1_score(label, label_estimated, average='macro')
    acc = np.sum(label == label_estimated) / label.size
    return acc, f1


def stat_results(label, results_estimated):
    label_estimated = np.argmax(results_estimated, 1)
    f1 = f1_score(label, label_estimated, average='macro')
    acc = np.sum(label == label_estimated) / label.size
    matrix = metrics.confusion_matrix(label, label_estimated) #, normalize='true'
    return acc, matrix, f1


def stat_acc_f1_tpn(label, label_estimated, task_num=5, threshold=0.5):
    label_new = []
    label_estimated_new = []
    for i in range(label.size):
        if label[i] == 0:
            label_new.append(np.zeros((task_num, 1)))
            label_estimated_new_temp = np.zeros((task_num, 1))
            label_estimated_new_temp[label_estimated[i, :] > threshold] = 1
            label_estimated_new.append(label_estimated_new_temp)
        else:
            label_new.append(np.ones((1, 1)))
            label_estimated_new_temp = np.zeros((1, 1))
            label_estimated_new_temp[label_estimated[i, label[i] - 1] > threshold] = 1
            label_estimated_new.append(label_estimated_new_temp)
    label_new = np.concatenate(label_new, 0)[:, 0]
    label_estimated_new = np.concatenate(label_estimated_new, 0)[:, 0]
    f1 = f1_score(label_new, label_estimated_new, average='macro')
    acc = np.sum(label_new == label_estimated_new) / label_new.size
    return acc, f1


def compute_dtw_metric(label, results_estimated):
    dtw_dist = 0
    for S in range(len([label[0]])):
        temp, path = fastdtw(label[S].flatten(), results_estimated[S].flatten())
        dtw_dist+=temp
    return dtw_dist


def compute_levenschtein_distance(label, results_estimated):
    lev_distance = Levenshtein.distance(label, results_estimated)
    return lev_distance


def compute_euclidean_distance(label, results_estimated):
    edist = 0
    for S in range(len([label[0]])):
        edist += euclidean(label[S].flatten(), results_estimated[S].flatten())
    #return np.sum(np.linalg.norm(np.array(label) - np.array(results_estimated), axis=2))
    return edist
