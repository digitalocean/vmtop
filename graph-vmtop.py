#!/usr/bin/python3

import argparse
import os
import csv
import datetime
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.pyplot import figure

parser = argparse.ArgumentParser(description='Graph from vmtop')

parser.add_argument('-p', '--path', type=str, required=True,
                   help='folder containing the CSV files produced by vmtop')
parser.add_argument('-t', '--title', type=str,
        help='Title of the graph')
parser.add_argument('-f', '--filename', type=str, required=True,
                    help='csv list of input files to graph')
parser.add_argument('-m', '--metric', type=str, required=True,
                    help='csv list of metrics to graph')
parser.add_argument('-s', '--separate', action='store_true',
                    help='Make 1 chart per metric')
parser.add_argument('-b', '--begin', type=str,
                    help='Begin timestamp (%Y-%m-%d %H:%M:%S)')
parser.add_argument('-e', '--end', type=str,
                    help='Begin timestamp (%Y-%m-%d %H:%M:%S)')

args = parser.parse_args()

if args.begin:
    args.begin = datetime.datetime.strptime(args.begin, "%Y-%m-%d %H:%M:%S")
if args.end:
    args.end = datetime.datetime.strptime(args.end, "%Y-%m-%d %H:%M:%S")

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
        dt = datetime.datetime.strptime(row['timestamp'], "%Y-%m-%d %H:%M:%S")
        if args.begin is not None and dt < args.begin:
            continue
        if args.end is not None and dt > args.end:
            break
        files[f]['timestamp'].append(dt)
        for m in metrics:
            if m == 'timestamp':
                continue
            files[f][m].append(float(row[m]))

# If we have more than 1 metric, generate 1 image with multiple graphs
if len(metrics) > 2 and not args.separate:
    fig, axs = plt.subplots(len(metrics) - 1, sharex=True)
    if args.title:
        fig.suptitle(args.title)
    fig.set_figwidth(16)
    fig.set_figheight(3 * (len(metrics) - 1))

    i = 0
    for m in metrics:
        if m == 'timestamp':
            continue

        axs[i].set(xlabel='Time', ylabel=m)
        for f in files.keys():
            axs[i].plot(files[f]['timestamp'], files[f][m], label=f)

        i += 1
    out_file = os.path.join(args.path, "multi.png")
    fig.savefig(out_file)
    plt.close()
    print("Generated %s" % out_file)
else:
    for m in metrics:
        if m == 'timestamp':
            continue
        fig = plt.figure(figsize=(16,9))
        if args.title:
            fig.suptitle(args.title)
        plt.xlabel('Time')
        plt.ylabel(m)
        for f in files.keys():
            plt.plot(files[f]['timestamp'], files[f][m], label=f)
        plt.gcf().autofmt_xdate()
        plt.legend()

        out_file = os.path.join(args.path, "%s.png" % m)
        fig.savefig(out_file)
        plt.close()
        print("Generated %s" % out_file)

