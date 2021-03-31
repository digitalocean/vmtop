#!/usr/bin/python3

import argparse
import os
import csv
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.pyplot import figure

parser = argparse.ArgumentParser(description='Graph from vmtop')

parser.add_argument('-p', '--path', type=str, required=True,
                   help='folder containing the CSV files produced by vmtop')
parser.add_argument('-f', '--filename', type=str, required=True,
                    help='csv list of input files to graph')
parser.add_argument('-m', '--metric', type=str, required=True,
                    help='csv list of metrics to graph')

args = parser.parse_args()

metrics = ['timestamp']
for m in args.metric.split(','):
    metrics.append(m)

files = {}
for v in args.filename.split(','):
    files[v] = {}
    for m in metrics:
        files[v][m] = []


for f in files.keys():
    fpath = os.path.join(args.path, "%s.csv" % f)

    reader = csv.DictReader(open(fpath, 'r'))
    for row in reader:
        for m in metrics:
            if m == 'timestamp':
                files[f][m].append(row[m])
            else:
                files[f][m].append(float(row[m]))
    files[f]['timestamp'] = pd.to_datetime(files[f]['timestamp'],
            format="%Y-%m-%d %H:%M:%S")

# Create 1 graph per metric with all the VMs on the same graph
for m in metrics:
    if m == 'timestamp':
        continue

    fig = plt.figure(figsize=(16,9))
    plt.xlabel('Time')
    plt.ylabel(m)
    plt.title('%s over time' % m)
    for f in files.keys():
        plt.plot(files[f]['timestamp'], files[f][m], label=f)
    plt.gcf().autofmt_xdate()
    plt.legend()

    fig.savefig(os.path.join(args.path, "%s.png" % m))
    plt.close()

