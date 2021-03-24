#!/usr/bin/python3

# TODO:
#  - ALLOC BY NODE
#  - find node by grep cpuset /proc/54606/cgroup
#  - don't iterate by node, make vcpus account in the node
#    - if a VM belongs to multiple nodes, show it in both + warning
#    - confirm node at each poll
#  - scan for new/old VMs
#  - show VM sizes
#  - filter by name, not just PID
#  - add I/O stats per VM

import subprocess
import operator
import datetime
import time
import os
import sys
import argparse
from time import sleep

class QemuThread:
    def __init__(self, vm_pid, vcpu_pid):
        self.vm_pid = vm_pid
        self.vcpu_pid = vcpu_pid
        self.last_stealtime = None
        self.last_cputime = None
        self.last_scrape_ts = None
        self.pc_steal = 0.0
        self.pc_util = 0.0
        self.diff_steal = 0
        self.diff_util = 0
        self.diff_ts = 0
        self.get_vcpu_name()
        self.get_schedstats()

    def get_vcpu_name(self):
        with open('/proc/%s/task/%s/comm' % (self.vm_pid, self.vcpu_pid), 'r') as f:
            self.vcpu_name = f.read()

    def __repr__(self):
        return "%s (%s), util: %0.02f %%, steal: %0.02f %%" % (self.vcpu_name, self.vcpu_pid, self.pc_util, self.pc_steal)

    def get_schedstats(self):
        with open('/proc/%s/task/%s/schedstat' % (self.vm_pid, self.vcpu_pid), 'r') as f:
            stats = f.read().split(' ')
            self.last_cputime = int(stats[0])
            self.last_stealtime = int(stats[1])
            self.last_scrape_ts = time.time() * 1000000000

    def refresh_stats(self):
        prev_steal_time = self.last_stealtime
        prev_cpu_time = self.last_cputime
        prev_scrape_ts = self.last_scrape_ts
        self.get_schedstats()
        self.diff_ts = self.last_scrape_ts - prev_scrape_ts
        self.diff_steal = self.last_stealtime - prev_steal_time
        self.diff_util = self.last_cputime - prev_cpu_time
        self.pc_util = self.diff_util / self.diff_ts * 100
        self.pc_steal = self.diff_steal / self.diff_ts * 100


class VM:
    def __init__(self, args, vm_pid):
        self.args = args
        self.vm_pid = vm_pid
        self.name = None
        self.mem = 0
        self.vcpu_count = 0
        self.vcpus = {}
        self.emulators = {}
        self.get_name()
        self.get_threads()
        self.get_node()

    def __repr__(self):
        vm = "  - %s (%s), vcpu util: %0.02f%%, vcpu steal: %0.02f%%, emulators util: %0.02f%%, emulators steal: %0.02f%%" % (
                self.name, self.vm_pid,
                self.vcpu_sum_pc_util,
                self.vcpu_sum_pc_steal,
                self.emulators_sum_pc_util,
                self.emulators_sum_pc_steal)

        if self.args.vcpu:
            vcpu_util = ""
            for v in self.vcpus.values():
                vcpu_util = "%s\n    - %s" % (vcpu_util, v)
            emulators_util = ""
            if self.args.emulators:
                for v in self.emulators.values():
                    emulators_util = "%s\n    - %s" % (emulators_util, v)
            return "%s%s%s" % (vm, vcpu_util, emulators_util)
        else:
            return vm

    def __str__(self):
        return self.__repr__()

    def get_threads(self):
        for tid in os.listdir('/proc/%s/task/' % self.vm_pid):
            fname = '/proc/%s/task/%s/comm' % (self.vm_pid, tid)
            with open(fname, 'r') as _f:
                comm = _f.read()
                if 'CPU' in comm:
                    vcpu_pid = int(tid)
                    self.vcpus[vcpu_pid] = QemuThread(self.vm_pid, vcpu_pid)
                else:
                    emulator_pid = int(tid)
                    try:
                        self.emulators[emulator_pid] = QemuThread(self.vm_pid, emulator_pid)
                    except:
                        # Ignore threads that disappear (workers)
                        pass

    def get_name(self):
        with open('/proc/%s/cmdline' % self.vm_pid, mode='r') as fh:
            cmdline = fh.read().split('\0')
        for i in range(len(cmdline)):
            if cmdline[i].startswith('guest='):
                self.name = cmdline[i].split('=')[1].split(',')[0]
            if cmdline[i] == '-m':
                self.mem = int(cmdline[i+1])
            if cmdline[i] == '-smp':
                self.vcpu_count = int(cmdline[i+1].split(',')[0])

    def get_node(self):
        # Try to extract Node0, Node1, Total, if only 2 fields, we only have
        # 1 numa node
        cmd = "numastat -p %s |grep ^Total | awk '{print $2,$3,$4}'" % self.vm_pid
        usage = subprocess.check_output(cmd, shell=True).strip().decode("utf-8").split(' ')
        if len(usage) == 2:
            self.node = 0
            return
        nodes_usage = [float(usage[0]), float(usage[1])]
        if nodes_usage[0] > nodes_usage[1]:
            self.node = 0
        else:
            self.node = 1

    def refresh_stats(self):
        # sum of all vcpu stats
        self.vcpu_sum_pc_util = 0
        self.vcpu_sum_pc_steal = 0
        for vcpu in self.vcpus.keys():
            self.vcpus[vcpu].refresh_stats()
            self.vcpu_sum_pc_util += self.vcpus[vcpu].pc_util
            self.vcpu_sum_pc_steal += self.vcpus[vcpu].pc_steal

        self.emulators_sum_pc_util = 0
        self.emulators_sum_pc_steal = 0
        to_remove = []
        for emulators in self.emulators.keys():
            try:
                self.emulators[emulators].refresh_stats()
            except:
                to_remove.append(self.emulators[emulators])
                continue
            self.emulators_sum_pc_util += self.emulators[emulators].pc_util
            self.emulators_sum_pc_steal += self.emulators[emulators].pc_steal
        # workers are added/removed on demand, so we can't keep track of all
        for r in to_remove:
            del self.emulators[emulators]


class FixSteal:
    def __init__(self):
        # FIXME: need a Node class
        self.vms_per_node = {}
        self.mem_per_node = {}
        self.vcpus_per_node = {}
        self.parse_args()
        print("Collecting VM informations...")
        self.get_info()
        self.list_vms()
        nr_vms = ""
        for node in self.vms_per_node.keys():
            print(" - %s VMs (%s vcpus, %s GB mem) on node %s" % (
                len(self.vms_per_node[node]), self.vcpus_per_node[node],
                self.mem_per_node[node]/1024, node))
        self.loop()

    def parse_args(self):
        parser = argparse.ArgumentParser(description='Monitor local steal')
        parser.add_argument('-r', '--refresh', type=int, default=1,
                help='refresh rate (seconds)')
        parser.add_argument('-l', '--limit', type=int,
                help='limit to top X VMs per node')
        parser.add_argument('-s', '--sort', type=str,
                choices=['vcpu_util', 'vcpu_steal',
                    'emulators_util', 'emulators_steal'],
                default='vcpu_util',
                help='sort order for VM list, default: vcpu_util')
        parser.add_argument('-p', '--pid', type=str,
                help='Limit to pid (csv), implies --vm')
        parser.add_argument('--vcpu', action='store_true',
                help='show vcpu stats (implies --vm)')
        parser.add_argument('--emulators', action='store_true',
                help='show emulators stats (implies --vm)')
        parser.add_argument('--vm', action='store_true',
                help='show vm stats')
        parser.add_argument('--node', type=str,
                help='Limit to specific NUMA node (csv)')
        self.args = parser.parse_args()
        if self.args.vcpu is True or self.args.emulators is True:
            self.args.vm = True
        if self.args.pid is not None:
            self.args.vm = True
            self.args.pids = []
            for i in self.args.pid.split(','):
                self.args.pids.append(int(i))
        # Sort mapping to variable name
        if self.args.sort == 'vcpu_util':
            self.args.sort = 'vcpu_sum_pc_util'
        elif self.args.sort == 'vcpu_steal':
            self.args.sort = 'vcpu_sum_pc_steal'
        elif self.args.sort == 'emulators_util':
            self.args.sort = 'emulators_sum_pc_util'
        elif self.args.sort == 'emulators_steal':
            self.args.sort = 'emulators_sum_pc_steal'

        # filter by node
        if self.args.node is not None:
            nodes = []
            for n in self.args.node.split(','):
                nodes.append(int(n))
            self.args.node = nodes

    def loop(self):
        while True:
            time.sleep(self.args.refresh)
            print("\n%s" % datetime.datetime.today())
            node_stats = {}
            for node in self.vms_per_node.keys():
                nr_hwthreads = len(self.numa_nodes_hwthreads[node])
                node_stats[node] = {}
                node_stats[node]['node_vcpu_sum_pc_util'] = 0
                node_stats[node]['node_vcpu_sum_pc_steal'] = 0
                node_stats[node]['node_emulators_sum_pc_util'] = 0
                node_stats[node]['node_emulators_sum_pc_steal'] = 0
                for v in self.vms_per_node[node].keys():
                    vm = self.vms_per_node[node][v]
                    vm.refresh_stats()
                    node_stats[node]['node_vcpu_sum_pc_util'] += vm.vcpu_sum_pc_util
                    node_stats[node]['node_vcpu_sum_pc_steal'] += vm.vcpu_sum_pc_steal
                    node_stats[node]['node_emulators_sum_pc_util'] += vm.emulators_sum_pc_util
                    node_stats[node]['node_emulators_sum_pc_steal'] += vm.emulators_sum_pc_steal

            for node in self.vms_per_node.keys():
                if self.args.node is not None:
                    if node not in self.args.node:
                        continue
                nr = 0
                if self.args.vm:
                    print("Node %d:" % node)
                for vm in (sorted(self.vms_per_node[node].values(),
                    key=operator.attrgetter(self.args.sort), reverse=True)):
                    if self.args.vm:
                        if self.args.pid is not None:
                            if vm.vm_pid in self.args.pids:
                                print(vm)
                        else:
                            if self.args.limit is not None and nr > self.args.limit:
                                break
                            print(vm)
                            nr += 1
                print("  Node %d total: vcpu util: %0.02f%%, vcpu steal: %0.02f%%, emulators util: %0.02f%%, emulators steal: %0.02f%%" % (
                    node,
                    node_stats[node]['node_vcpu_sum_pc_util'] / nr_hwthreads,
                    node_stats[node]['node_vcpu_sum_pc_steal'] / nr_hwthreads,
                    node_stats[node]['node_emulators_sum_pc_util'] / nr_hwthreads,
                    node_stats[node]['node_emulators_sum_pc_steal'] / nr_hwthreads))

    def get_info(self):
        self.numa_nodes_hwthreads = []
        cmd = "lscpu | grep NUMA | grep CPU | awk '{print $4}'"
        count = subprocess.check_output(cmd, shell=True).strip().decode("utf-8")
        for i in count.split("\n"):
            node_hwthread_list = []
            _i = self.__mixrange(i)
            for j in _i:
                node_hwthread_list.append(int(j))
            self.numa_nodes_hwthreads.append(node_hwthread_list)

    def list_vms(self):
        cmd = "pgrep qemu"
        pids = subprocess.check_output(cmd, shell=True).strip().decode("utf-8").split('\n')
        for pid in pids:
            v = VM(self.args, pid)
            if v.node not in self.vms_per_node:
                self.vms_per_node[v.node] = {}
                self.mem_per_node[v.node] = 0
                self.vcpus_per_node[v.node] = 0
            self.vms_per_node[v.node][pid] = v
            self.mem_per_node[v.node] += v.mem
            self.vcpus_per_node[v.node] += v.vcpu_count

    def __mixrange(self, s):
        r = []
        for i in s.split(","):
            if "-" not in i:
                r.append(int(i))
            else:
                l, h = map(int, i.split("-"))
                r += range(l, h + 1)
        return r

    def run(self):
        pass

if os.geteuid() != 0:
    print("Need to run as root")
    sys.exit(1)

s = FixSteal()
s.run()
