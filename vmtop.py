#!/usr/bin/python3

# TODO:
#  - vhost
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


def mixrange(s):
    r = []
    for i in s.split(","):
        if "-" not in i:
            r.append(int(i))
        else:
            l, h = map(int, i.split("-"))
            r += range(l, h + 1)
    return r


class QemuThread:
    def __init__(self, vm_pid, thread_pid, machine):
        self.vm_pid = vm_pid
        self.machine = machine
        self.thread_pid = thread_pid
        self.last_stealtime = None
        self.last_cputime = None
        self.last_scrape_ts = None
        self.nodes = None
        self.pc_steal = 0.0
        self.pc_util = 0.0
        self.diff_steal = 0
        self.diff_util = 0
        self.diff_ts = 0
        self.get_thread_name()
        self.get_thread_cpuset()
        self.get_schedstats()

    def get_thread_name(self):
        with open('/proc/%s/task/%s/comm' % (self.vm_pid, self.thread_pid),
                  'r') as f:
            self.thread_name = f.read()

    def get_thread_cpuset(self):
        with open('/proc/%s/task/%s/cpuset' % (self.vm_pid, self.thread_pid),
                  'r') as f:
            self.cpuset = f.read().strip()
        self.nodes = self.machine.get_nodes(self.cpuset)
        if len(self.nodes) > 1:
            print("Warning: VCPU %d from VM %d belongs to multiple nodes, "
                  "node utilization may be inaccurate" % (self.thread_pid,
                                                          self.vm_pid))

    def __repr__(self):
        return "%s (%s), util: %0.02f %%, steal: %0.02f %%" % (
                self.thread_name, self.thread_pid, self.pc_util,
                self.pc_steal)

    def get_schedstats(self):
        with open('/proc/%s/task/%s/schedstat' % (
                  self.vm_pid, self.thread_pid), 'r') as f:
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
    def __init__(self, args, vm_pid, machine):
        self.args = args
        self.vm_pid = vm_pid
        self.machine = machine
        self.name = None
        self.mem_allocated = 0
        # We assume all the allocated memory is/will be allocated on
        # only one node
        self.primary_node = None
        self.mem_used_per_node = {}
        self.total_vcpu_count = 0
        self.total_vcpu_count_per_node = {}

        self.vcpu_threads = {}
        self.emulator_threads = {}
        self.get_vm_info()
        self.get_threads()
        self.get_node_memory()

    def __repr__(self):
        vm = "  - %s (%s), vcpu util: %0.02f%%, vcpu steal: %0.02f%%, " \
             "emulators util: %0.02f%%, emulators steal: %0.02f%%" % (
                self.name, self.vm_pid,
                self.vcpu_sum_pc_util,
                self.vcpu_sum_pc_steal,
                self.emulators_sum_pc_util,
                self.emulators_sum_pc_steal)

        if self.args.vcpu:
            vcpu_util = ""
            for v in self.vcpu_threads.values():
                vcpu_util = "%s\n    - %s" % (vcpu_util, v)
            emulators_util = ""
            if self.args.emulators:
                for v in self.emulator_threads.values():
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
            tid = int(tid)
#            try:
            thread = QemuThread(self.vm_pid, tid, self.machine)
#            except:
#                # Ignore threads that disappear for now (temporary workers)
#                continue
            if 'CPU' in comm:
                self.vcpu_threads[tid] = thread
                for n in thread.nodes:
                    n.vcpu_threads[tid] = thread
            else:
                self.emulator_threads[tid] = thread
                for n in thread.nodes:
                    n.emulator_threads[tid] = thread

    def get_vm_info(self):
        with open('/proc/%s/cmdline' % self.vm_pid, mode='r') as fh:
            cmdline = fh.read().split('\0')
        for i in range(len(cmdline)):
            if cmdline[i].startswith('guest='):
                self.name = cmdline[i].split('=')[1].split(',')[0]
            if cmdline[i] == '-m':
                self.mem_allocated = int(cmdline[i+1])
            if cmdline[i] == '-smp':
                self.total_vcpu_count = int(cmdline[i+1].split(',')[0])

    def get_node_memory(self):
        cmd = ["numastat", "-p", str(self.vm_pid)]
        usage = subprocess.check_output(
                cmd,
                shell=False).decode("utf-8").split('\n')[-2].split()[1:-1]
        maxnode = None
        maxmem = 0
        for node_id in range(len(usage)):
            mem = float(usage[node_id])
            self.mem_used_per_node[node_id] = mem
            self.machine.nodes[node_id].vm_mem_used += mem
            if maxnode is None or mem > maxmem:
                maxnode = node_id
                maxmem = mem
        self.primary_node = self.machine.nodes[maxnode]
        self.primary_node.vm_mem_allocated += self.mem_allocated

    def refresh_stats(self):
        # sum of all vcpu stats
        self.vcpu_sum_pc_util = 0
        self.vcpu_sum_pc_steal = 0
        for vcpu in self.vcpu_threads.values():
            vcpu.refresh_stats()
            self.vcpu_sum_pc_util += vcpu.pc_util
            self.vcpu_sum_pc_steal += vcpu.pc_steal

        self.emulators_sum_pc_util = 0
        self.emulators_sum_pc_steal = 0
        to_remove = []
        for emulator in self.emulator_threads.values():
            try:
                emulator.refresh_stats()
            except Exception:
                to_remove.append(emulator)
                continue
            self.emulators_sum_pc_util += emulator.pc_util
            self.emulators_sum_pc_steal += emulator.pc_steal
        # workers are added/removed on demand, so we can't keep track of all
        # FIXME
#        for r in to_remove:
#            del emulators
        self.primary_node.vcpu_sum_pc_util += self.vcpu_sum_pc_util
        self.primary_node.emulators_sum_pc_util += self.emulators_sum_pc_util
        self.primary_node.vcpu_sum_pc_steal += self.vcpu_sum_pc_steal
        self.primary_node.emulators_sum_pc_steal += self.emulators_sum_pc_steal


class Node:
    def __init__(self, _id):
        self.id = _id
        self.hwthread_list = []
        self.clear()

    def clear(self):
        # Approximation, VMs could be split between nodes, we use the node
        # where most of the memory is allocated to decide here
        self.node_vms = {}
        # Approximation, assumes all the allocated memory is not split
        self.vm_mem_allocated = 0
        self.vm_mem_used = 0
        self.vcpu_threads = {}
        self.emulator_threads = {}
        self.clear_stats()

    def clear_stats(self):
        self.vcpu_sum_pc_util = 0
        self.vcpu_sum_pc_steal = 0
        self.emulators_sum_pc_util = 0
        self.emulators_sum_pc_steal = 0

    def refresh_stats(self):
        for vm in self.node_vms.values():
            vm.refresh_stats()

    def print_node_initial_count(self):
        print(" - %s VMs (%s vcpus, %0.02f GB mem allocated, "
              "%0.02f GB mem used) on node %s" % (
                self.nr_vms, len(self.vcpu_threads),
                self.vm_mem_allocated/1024, self.vm_mem_used/1024, self.id))

    @property
    def nr_vms(self):
        return len(self.node_vms)

    @property
    def nr_hwthreads(self):
        return len(self.hwthread_list)


class Machine:
    def __init__(self, args):
        self.args = args
        self.nodes = {}
        self.all_vms = {}
        self.get_cpuset_mount_point()

    def get_cpuset_mount_point(self):
        with open('/proc/mounts', 'r') as f:
            mounts = f.read().split('\n')
        for m in mounts:
            m = m.split()
            if m[0] == 'cgroup':
                if 'cpuset' in m[3]:
                    self.cpuset_mount_point = m[1]
                    return

    def clear_nodes(self):
        for n in self.nodes.keys():
            self.nodes[n].clear()

    def clear_stats(self):
        for n in self.nodes.keys():
            self.nodes[n].clear_stats()

    def refresh_stats(self):
        # XXX: refresh vm list and pinning ?
        for node in self.nodes.values():
            node.clear_stats()
            node.refresh_stats()

    def print_initial_count(self):
        for node in self.nodes.values():
            node.print_node_initial_count()

    def get_nodes(self, cpuset):
        nodes = []
        fullpath = "%s/%s/cpuset.cpus" % (self.cpuset_mount_point, cpuset)
        with open(fullpath, 'r') as f:
            cpus = mixrange(f.read())
        for c in cpus:
            for n in self.nodes.values():
                if c in n.hwthread_list:
                    if n not in nodes:
                        nodes.append(n)
                    break
        return nodes

    @property
    def nr_nodes(self):
        return len(self.nodes)

    @property
    def nr_hwthreads(self):
        nr = 0
        for n in self.nodes.values():
            nr += len(n.hwthread_list)
        return nr

    def get_info(self):
        cmd = "lscpu | egrep 'NUMA node[0-9]+ CPU'"
        numa_info = subprocess.check_output(
                cmd, shell=True).strip().decode("utf-8")
        for i in numa_info.split("\n"):
            node_id = int(i.split(' ')[1].replace('node', ''))
            node = Node(node_id)
            hwthreads = i.split(':')[1].replace(' ', '')
            _i = mixrange(hwthreads)
            for j in _i:
                node.hwthread_list.append(int(j))
            self.nodes[node_id] = node

    def list_vms(self):
        cmd = ["pgrep", "qemu"]
        try:
            pids = subprocess.check_output(
                    cmd, shell=False).strip().decode("utf-8").split('\n')
        except subprocess.CalledProcessError:
            print("No VMs")
            sys.exit(0)
        self.clear_nodes()
        for pid in pids:
            pid = int(pid)
            v = VM(self.args, pid, self)
            self.all_vms[pid] = v
            v.primary_node.node_vms[pid] = v


class VmTop:
    def __init__(self):
        self.parse_args()
        self.machine = Machine(self.args)
        print("Collecting VM informations...")
        self.machine.get_info()
        self.machine.list_vms()
        self.machine.print_initial_count()
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
            self.machine.clear_stats()
            self.machine.refresh_stats()

            for node in self.machine.nodes.keys():
                if self.args.node is not None:
                    if node not in self.args.node:
                        continue
                nr = 0
                node = self.machine.nodes[node]
                if self.args.vm:
                    print("Node %d:" % node.id)
                for vm in (sorted(node.node_vms.values(),
                                  key=operator.attrgetter(self.args.sort),
                                  reverse=True)):
                    if self.args.vm:
                        if self.args.pid is not None:
                            if vm.vm_pid in self.args.pids:
                                print(vm)
                        else:
                            if self.args.limit is not None and \
                                    nr >= self.args.limit:
                                break
                            print(vm)
                            nr += 1
                print("  Node %d total: vcpu util: %0.02f%%, "
                      "vcpu steal: %0.02f%%, emulators util: %0.02f%%, "
                      "emulators steal: %0.02f%%" % (
                          node.id,
                          node.vcpu_sum_pc_util / node.nr_hwthreads,
                          node.vcpu_sum_pc_steal / node.nr_hwthreads,
                          node.emulators_sum_pc_util / node.nr_hwthreads,
                          node.emulators_sum_pc_steal / node.nr_hwthreads))

    def run(self):
        pass


if os.geteuid() != 0:
    print("Need to run as root")
    sys.exit(1)

s = VmTop()
s.run()
