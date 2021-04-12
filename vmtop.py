#!/usr/bin/python3

# TODO:
#  - system-wide metrics
#  - /sys/devices/system/node/nodeX/numastat
#  - can we get guest throttling data (for live migration) ?
#  - user-controlled list metrics to show, the list is getting large
#  - vmexit rate + reasons
#  - arbitrary groups of processes metrics (kernel threads,
#    middleware, etc)
#  - parallel scrape
#  - filter by name, not just PID

import subprocess
import operator
import threading
import time
import signal
import os
import sys
import argparse
import shutil
from datetime import datetime


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
    def __init__(self, vm_pid, cgroup, thread_pid, machine, vhost=False):
        self.vm_pid = vm_pid
        self.machine = machine
        self.thread_pid = thread_pid
        self.last_stealtime = None
        self.last_cputime = None
        self.last_scrape_ts = None
        self.nodes = None
        self.vhost = vhost
        self.cgroup = cgroup
        self.pc_steal = 0.0
        self.pc_util = 0.0
        self.diff_steal = 0
        self.diff_util = 0
        self.diff_ts = 0
        self.warned = False
        self.get_thread_name()
        self.get_thread_cpuset()
        self.get_schedstats()

    def get_thread_name(self):
        if self.vhost is True:
            fpath = '/proc/%s/comm' % (self.thread_pid)
        else:
            fpath = '/proc/%s/task/%s/comm' % (self.vm_pid, self.thread_pid)
        with open(fpath, 'r') as f:
            self.thread_name = f.read().strip()

    def get_thread_cpuset(self):
        try:
            if self.vhost is not True:
                fpath = '/proc/%s/task/%s/cpuset' % (self.vm_pid, self.thread_pid)
            with open(fpath, 'r') as f:
                line = f.read().strip()
                # Cgroup1
                if self.cgroup != 2:
                    self.cpuset = line
                else:
                    # Cgroup2
                    # If cpuset controller is not enabled, then it may just
                    # return "/machine.slice" for the /proc/<pid>/cpuset
                    if line != "/machine.slice":
                        self.cpuset = line
                    else:
                        print("Please check if cpuset controller is enabled")
                        sys.exit(1)
        except:
            # Teardown
            self.nodes = []
            return
        self.nodes = self.machine.get_nodes(self.cpuset)

        if len(self.nodes) != 1:
            # kvm-pit is not pinned, but also mostly idle, no need to
            # warn here
            if 'kvm-pit' in self.thread_name:
                return
            if self.warned is True:
                return
            print("Warning: thread %d from VM %d belongs to multiple nodes, "
                  "node accounting may be inaccurate" % (self.thread_pid,
                      self.vm_pid))
            self.warned = True

    def get_schedstats(self):
        self.last_scrape_ts = time.time() * 1000000000
        if self.vhost is True:
            fpath = '/proc/%s/schedstat' % (self.thread_pid)
        else:
            fpath = '/proc/%s/task/%s/schedstat' % (self.vm_pid, self.thread_pid)
        try:
            with open(fpath, 'r') as f:
                stats = f.read().split(' ')
        except FileNotFoundError:
            # On VM teardown return 0
            self.last_cputime = 0
            self.last_stealtime = 0
            return
        self.last_cputime = int(stats[0])
        self.last_stealtime = int(stats[1])

    def __repr__(self):
        return "%s (%s), util: %0.02f %%, steal: %0.02f %%" % (
                self.thread_name, self.thread_pid, self.pc_util,
                self.pc_steal)

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


class NIC:
    def __init__(self, vm, name):
        self.vm = vm
        self.name = name
        self.last_scrape_ts = None
        self.last_rx = None
        self.last_tx = None
        self.last_rx_drop = None
        self.last_tx_drop = None
        self.tx_rate = None
        self.rx_rate = None
        self.tx_rate_dropped = None
        self.rx_rate_dropped = None
        self.get_stats()

    def get_stats(self):
        self.last_scrape_ts = time.time()
        # Flipped rx/tx to reflect the VM point of view
        try:
            with open('/sys/devices/virtual/net/%s/statistics/tx_bytes' %
                      self.name, 'r') as f:
                self.last_rx = int(f.read().strip())
            with open('/sys/devices/virtual/net/%s/statistics/rx_bytes' %
                      self.name, 'r') as f:
                self.last_tx = int(f.read().strip())
            with open('/sys/devices/virtual/net/%s/statistics/tx_dropped' %
                      self.name, 'r') as f:
                self.last_rx_dropped = int(f.read().strip())
            with open('/sys/devices/virtual/net/%s/statistics/rx_dropped' %
                      self.name, 'r') as f:
                self.last_tx_dropped = int(f.read().strip())
        except:
            # VM Teardown
            self.last_rx = 0
            self.last_tx = 0
            self.last_rx_dropped = 0
            self.last_tx_dropped = 0
            return

    def refresh_stats(self):
        prev_scrape_ts = self.last_scrape_ts
        prev_rx = self.last_rx
        prev_tx = self.last_tx
        prev_rx_dropped = self.last_rx_dropped
        prev_tx_dropped = self.last_tx_dropped
        self.get_stats()
        diff_sec = self.last_scrape_ts - prev_scrape_ts
        mb = 1024.0 * 1024.0
        self.rx_rate = ((self.last_rx - prev_rx) * 8) / diff_sec / mb
        self.tx_rate = ((self.last_tx - prev_tx) * 8) / diff_sec / mb
        self.rx_rate_dropped = (self.last_rx_dropped - prev_rx_dropped) / diff_sec
        self.tx_rate_dropped = (self.last_tx_dropped - prev_tx_dropped) / diff_sec


class VM:
    def __init__(self, args, vm_pid, machine):
        self.args = args
        self.vm_pid = vm_pid
        self.machine = machine
        self.name = None
        self.csv = None
        # If the memory is on a different primary node than the vcpus
        self.warned_vcpu_mem_split = False
        # If the vcpus of that VM are running on different nodes
        self.warned_vcpu_split = False
        self.mem_allocated = 0
        self.clear_stats()

        # We assume all the allocated memory was allocated to fit on
        # only one node, we still track the real usage on each node.
        # changes protected by machine.nodes_lock.
        self.mem_primary_node = None
        # If a VM changed node, store it here and update with
        # machine.nodes_lock is held by the vm allocation thread
        self.new_mem_primary_node = None

        # Same thing with vcpus, we try to find the node where most
        # vcpus use and set it as default. If the VM is unpinned or
        # pinned to multiple nodes, we use mem_primary_node as an
        # assumption (and warn).
        # VMs are accounted for in vcpu_primary_node because this tool
        # first metric is CPU usage.
        self.vcpu_primary_node = None
        self.new_vcpu_primary_node = None

        self.mem_used_per_node = {}
        self.total_vcpu_count = 0
        self.total_vcpu_count_per_node = {}

        self.vcpu_threads = {}
        self.emulator_threads = {}
        self.vhost_threads = {}
        self.nics = {}

        self.last_io_scrape_ts = None
        self.last_io_read_bytes = None
        self.last_io_write_bytes = None

        self.get_vm_info()
        if self.args.csv is not None and self.args.vm is True:
            self.open_vm_csv()
        self.get_threads()
        self.get_node_memory()
        self.refresh_vcpu_primary_node()
        self.check_vcpu_mem_split()
        if self.args.no_nic is not True:
            self.get_nic_info()
        self.refresh_io_stats()

    @property
    def nr_vcpus(self):
        return len(self.vcpu_threads.keys())

    def set_vcpu_primary_node(self, new_node):
        if self.vcpu_primary_node == new_node:
            return
        if self.vcpu_primary_node is not None:
            # We cannot update the primary node directly, because of
            # concurrency between the 2 threads, so if the node needs
            # to be updated, set the new_ value and wait for the
            # alloc thread to update with the lock held.
            self.new_vcpu_primary_node = new_node
        else:
            self.vcpu_primary_node = new_node

    def refresh_vcpu_primary_node(self):
        tmp_primary = None
        for vcpu in self.vcpu_threads.values():
            # if any vcpu is floating to more than one node or we didn't
            # manage to find the node, assign it to the primary memory node
            # we already warned about this.
            if len(vcpu.nodes) != 1:
                self.set_vcpu_primary_node(self.mem_primary_node)
                break
            if tmp_primary is None:
                tmp_primary = vcpu.nodes[0]
            elif vcpu.nodes[0] != tmp_primary:
                self.set_vcpu_primary_node(self.mem_primary_node)
                if self.warned_vcpu_split is False:
                    print(f"Warning: VM {self.name} has vcpus pinned to "
                          f"different nodes, accounting will not be accurate")
                    self.warned_vcpu_split = True
                break
        self.set_vcpu_primary_node(tmp_primary)
        if self.vcpu_primary_node is None:
            print(f"Failed to set the vcpu primary node for VM {self.name}")

    def refresh_vcpu_node(self):
        for vcpu in self.vcpu_threads.values():
            vcpu.get_thread_cpuset()
        self.refresh_vcpu_primary_node()

    def check_vcpu_mem_split(self):
        if self.warned_vcpu_mem_split is True:
            return
        for vcpu in self.vcpu_threads.values():
            if len(vcpu.nodes) == 1 and vcpu.nodes[0] != self.mem_primary_node:
                print(f"Warning: VCPU thread {vcpu.thread_pid} from VM "
                      f"{self.name} is not pinned on the same node as its "
                      f"memory")
                self.warned_vcpu_mem_split = True

    def __str__(self):
        if self.args.vcpu:
            vm = "  - %s (%s), vcpu util: %0.02f%%, vcpu steal: %0.02f%%, " \
                    "vhost util: %0.02f%%, vhost steal: %0.02f%%" \
                 "emulators util: %0.02f%%, emulators steal: %0.02f%%" % (
                    self.name, self.vm_pid,
                    self.vcpu_sum_pc_util,
                    self.vcpu_sum_pc_steal,
                    self.vhost_sum_pc_util,
                    self.vhost_sum_pc_steal,
                    self.emulators_sum_pc_util,
                    self.emulators_sum_pc_steal)
            vcpu_util = ""
            for v in self.vcpu_threads.values():
                vcpu_util = "%s\n    - %s" % (vcpu_util, v)
            emulators_util = ""
            if self.args.emulators:
                for v in self.emulator_threads.values():
                    emulators_util = "%s\n    - %s" % (emulators_util, v)
            return "%s%s%s" % (vm, vcpu_util, emulators_util)
        else:
            return self.args.vm_format.format(
                    self.name, str(self.vm_pid),
                    "%0.02f" % self.vcpu_sum_pc_util,
                    "%0.02f" % self.vcpu_sum_pc_steal,
                    "%0.02f" % self.vhost_sum_pc_util,
                    "%0.02f" % self.vhost_sum_pc_steal,
                    "%0.02f" % self.emulators_sum_pc_util,
                    "%0.02f" % self.emulators_sum_pc_steal,
                    "%0.02f" % self.mb_read,
                    "%0.02f" % self.mb_write,
                    "%0.02f" % self.rx_rate,
                    "%0.02f" % self.tx_rate,
                    "%0.02f" % self.rx_rate_dropped,
                    "%0.02f" % self.tx_rate_dropped)

    def open_vm_csv(self):
        fname = os.path.join(self.args.csv, "%s.csv" % self.name)
        self.csv = open(fname, 'w')
        self.csv.write("timestamp,pid,name,mem_node,vcpu_node,vcpu_util,vcpu_steal,emulators_util,"
                "emulators_steal,vhost_util,vhost_steal,disk_read,disk_write,rx,tx,"
                "rx_dropped,tx_dropped\n")

    def output_vm_csv(self, timestamp):
        # Output the CSV file
        # we use abs() because Python manages to write -0.00 once in a
        # while...
        self.csv.write(f"{datetime.fromtimestamp(timestamp)},"
                       f"{self.vm_pid},{self.name},"
                       f"{self.mem_primary_node.id},"
                       f"{self.vcpu_primary_node.id},"
                       f"{'%0.02f' % (abs(self.vcpu_sum_pc_util))},"
                       f"{'%0.02f' % (abs(self.vcpu_sum_pc_steal))},"
                       f"{'%0.02f' % (abs(self.emulators_sum_pc_util))},"
                       f"{'%0.02f' % (abs(self.emulators_sum_pc_steal))},"
                       f"{'%0.02f' % (abs(self.vhost_sum_pc_util))},"
                       f"{'%0.02f' % (abs(self.vhost_sum_pc_steal))},"
                       f"{'%0.02f' % (abs(self.mb_read))},"
                       f"{'%0.02f' % (abs(self.mb_write))},"
                       f"{'%0.02f' % (abs(self.rx_rate))},"
                       f"{'%0.02f' % (abs(self.tx_rate))},"
                       f"{'%0.02f' % (abs(self.rx_rate_dropped))},"
                       f"{'%0.02f' % (abs(self.tx_rate_dropped))}\n")

    def get_nic_info(self):
        for fd in os.listdir(f'/proc/{self.vm_pid}/fd/'):
            lname = f'/proc/{self.vm_pid}/fd/{fd}'
            try:
                link = os.readlink(lname)
            except OSError:
                continue

            if link == '/dev/net/tun':
                try:
                    with open(f'/proc/{self.vm_pid}/fdinfo/{fd}', 'r') as _f:
                        fdinfo = _f.read().split('\n')
                except FileNotFoundError:
                    # Ignore fd that vanished
                    continue

                for l in fdinfo:
                    l = l.split()
                    if len(l) ==2 and l[0] == 'iff:':
                        self.nics[l[1]] = NIC(self, l[1])

    def get_threads(self):
        for tid in os.listdir('/proc/%s/task/' % self.vm_pid):
            fname = '/proc/%s/task/%s/comm' % (self.vm_pid, tid)
            try:
                with open(fname, 'r') as _f:
                    comm = _f.read()
                tid = int(tid)
                thread = QemuThread(self.vm_pid, self.machine.cgroup, tid, self.machine)
            except FileNotFoundError:
                # Ignore threads that disappear for now (temporary workers)
                continue
            if 'CPU' in comm:
                self.vcpu_threads[tid] = thread
            else:
                self.emulator_threads[tid] = thread

        # Find vhost threads
        cmd = ["pgrep", str(self.vm_pid)]
        pids = subprocess.check_output(
                cmd, shell=False).strip().decode("utf-8").split('\n')
        for p in pids:
            tid = int(p)
            thread = QemuThread(self.vm_pid, self.machine.cgroup, tid, self.machine, vhost=True)
            self.vhost_threads[tid] = thread

    def get_vm_info(self):
        with open('/proc/%s/cmdline' % self.vm_pid, mode='r') as fh:
            cmdline = fh.read().split('\0')
        for i in range(len(cmdline)):
            if cmdline[i].startswith('guest='):
                self.name = cmdline[i].split('=')[1].split(',')[0]
            elif cmdline[i].startswith('-name'):
                self.name = cmdline[i+1]
            elif cmdline[i] == '-m':
                self.mem_allocated = int(cmdline[i+1])
            elif cmdline[i] == '-smp':
                self.total_vcpu_count = int(cmdline[i+1].split(',')[0])

        if self.name is None:
            print("Failed to parse guest name")
            self.name = "unknown"

    def refresh_io_stats(self):
        self.last_io_scrape_ts = time.time()
        try:
            with open('/proc/%s/io' % self.vm_pid, 'r') as f:
                stats = f.read().split('\n')
        except FileNotFoundError:
            # On VM teardown return 0
            self.last_io_read_bytes = 0
            self.last_io_write_bytes = 0
            return

        for l in stats:
            l = l.split(' ')
            if l[0] == 'read_bytes:':
                self.last_io_read_bytes = int(l[1])
            if l[0] == 'write_bytes:':
                self.last_io_write_bytes = int(l[1])

    def get_node_memory(self):
        cmd = ["numastat", "-p", str(self.vm_pid)]
        try:
            usage = subprocess.check_output(
                    cmd,
                    shell=False).decode("utf-8").split('\n')[-2].split()[1:-1]
        except subprocess.CalledProcessError:
            # ctrl-c
            return
        maxnode = None
        maxmem = 0
        for node_id in range(len(usage)):
            try:
                mem = float(usage[node_id])
            except ValueError:
                # Teardown
                return
            self.mem_used_per_node[node_id] = mem
            if maxnode is None or mem > maxmem:
                maxnode = node_id
                maxmem = mem

        if self.mem_primary_node is None:
            self.mem_primary_node = self.machine.nodes[maxnode]
        elif self.mem_primary_node != self.machine.nodes[maxnode]:
            self.new_mem_primary_node = self.machine.nodes[maxnode]

    def clear_stats(self):
        self.vcpu_sum_pc_util = 0
        self.vcpu_sum_pc_steal = 0
        self.vhost_sum_pc_util = 0
        self.vhost_sum_pc_steal = 0
        self.emulators_sum_pc_util = 0
        self.emulators_sum_pc_steal = 0

    def refresh_stats(self):
        self.clear_stats()
        # sum of all vcpu stats
        for vcpu in self.vcpu_threads.values():
            vcpu.refresh_stats()
            self.vcpu_sum_pc_util += vcpu.pc_util
            self.vcpu_sum_pc_steal += vcpu.pc_steal

        # vhost
        for vhost in self.vhost_threads.values():
            vhost.refresh_stats()
            self.vhost_sum_pc_util += vhost.pc_util
            self.vhost_sum_pc_steal += vhost.pc_steal

        # emulators
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

        # disk
        prev_io_scrape_ts = self.last_io_scrape_ts
        prev_io_read_bytes = self.last_io_read_bytes
        prev_io_write_bytes = self.last_io_write_bytes
        self.refresh_io_stats()
        diff_sec = self.last_io_scrape_ts - prev_io_scrape_ts
        mb = 1024.0*1024.0
        self.mb_read = (self.last_io_read_bytes - prev_io_read_bytes) / diff_sec / mb
        self.mb_write = (self.last_io_write_bytes - prev_io_write_bytes) / diff_sec / mb

        self.tx_rate = 0
        self.rx_rate = 0
        self.tx_rate_dropped = 0
        self.rx_rate_dropped = 0
        if not self.args.no_nic:
            for n in self.nics.values():
                n.refresh_stats()
                self.tx_rate += n.tx_rate
                self.rx_rate += n.rx_rate
                self.tx_rate_dropped += n.tx_rate_dropped
                self.rx_rate_dropped += n.rx_rate_dropped

        # copy to node stats
        self.vcpu_primary_node.vcpu_sum_pc_util += self.vcpu_sum_pc_util
        self.vcpu_primary_node.vcpu_sum_pc_steal += self.vcpu_sum_pc_steal
        self.vcpu_primary_node.vhost_sum_pc_util += self.vhost_sum_pc_util
        self.vcpu_primary_node.vhost_sum_pc_steal += self.vhost_sum_pc_steal
        self.vcpu_primary_node.emulators_sum_pc_util += self.emulators_sum_pc_util
        self.vcpu_primary_node.emulators_sum_pc_steal += self.emulators_sum_pc_steal


class Node:
    def __init__(self, _id, args):
        self.id = _id
        self.args = args
        self.hwthread_list = []
        # Approximation, VMs could be split between nodes, we use the node
        # where most of the memory is allocated to decide here
        self.node_vms = {}

        # After the initial scan, the resource accounting is owned by the
        # refresh_vm_allocation thread
        # Approximation, assumes all the allocated memory is not split
        self.vm_mem_allocated = 0
        self.vm_mem_used = 0
        self.node_vcpu_threads = 0

        self.clear_stats()

    def clear_stats(self):
        # Owned by the main loop
        self.vcpu_sum_pc_util = 0
        self.vcpu_sum_pc_steal = 0
        self.vhost_sum_pc_util = 0
        self.vhost_sum_pc_steal = 0
        self.emulators_sum_pc_util = 0
        self.emulators_sum_pc_steal = 0

    def refresh_vm_allocation(self):
        tmp_vm_mem_allocated = 0
        for vm in self.node_vms.values():
            vm.get_node_memory()
            vm.refresh_vcpu_node()
            tmp_vm_mem_allocated += vm.mem_allocated
        self.vm_mem_allocated = tmp_vm_mem_allocated

    def print_node_initial_count(self):
        if self.args.csv is not None:
            return
        return("%s VMs (%s vcpus, %0.02f GB mem allocated, "
              "%0.02f GB mem used)" % (
                self.nr_vms, self.node_vcpu_threads,
                self.vm_mem_allocated/1024, self.vm_mem_used/1024))

    def open_csv_file(self):
        fname = os.path.join(self.args.csv, 'node%d.csv' % self.id)
        print("Writing node %s data in %s" % (self.id, fname))
        self.node_csv = open(fname, 'w')
        self.node_csv.write("timestamp,id,nr_vms,nr_vcpus,vm_mem_allocated,"
                            "vm_mem_used,vcpu_util,vcpu_steal,emulators_util,"
                            "emulators_steal,vhost_util,vhost_steal\n")

    def output_node_csv(self, timestamp):
        self.node_csv.write(f"{datetime.fromtimestamp(timestamp)},"
                            f"{self.id},"
                            f"{self.nr_vms},"
                            f"{self.node_vcpu_threads},"
                            f"{self.vm_mem_allocated/1024},"
                            f"{self.vm_mem_used/1024},"
                            f"{'%0.02f' % (self.vcpu_sum_pc_util)},"
                            f"{'%0.02f' % (self.vcpu_sum_pc_steal)},"
                            f"{'%0.02f' % (self.emulators_sum_pc_util)},"
                            f"{'%0.02f' % (self.emulators_sum_pc_steal)},"
                            f"{'%0.02f' % (self.vhost_sum_pc_util)},"
                            f"{'%0.02f' % (self.vhost_sum_pc_steal)}\n")

    def output_allocation(self):
        print("  Node %d: %s" % (self.id, self.print_node_initial_count()))

    @property
    def nr_vms(self):
        return len(self.node_vms)

    @property
    def nr_hwthreads(self):
        return len(self.hwthread_list)


class Machine:
    def __init__(self, args):
        self.args = args

        # to read/update node_vms in each node, prevents concurrent
        # accesses by the main loop and the vm_allocation thread
        self.nodes_lock = threading.Lock()

        # Protect concurrent reads and writes to the all_vms dict
        self.all_vms_lock = threading.Lock()

        self.nodes = {}
        self.all_vms = {}
        self.cgroup = self.check_cgroup()
        self.get_cpuset_mount_point()
        self.cancel = False

        self.get_machine_stat()

    def refresh_machine_stats(self):
        nr_hwthreads = self.nr_hwthreads
        prev_user = self.last_cpu_user
        prev_nice = self.last_cpu_nice
        prev_system = self.last_cpu_system
        prev_idle = self.last_cpu_idle
        prev_iowait = self.last_cpu_iowait
        prev_irq = self.last_cpu_irq
        prev_softirq = self.last_cpu_softirq
        prev_guest = self.last_cpu_guest
        prev_ts = self.last_scrape_ts
        self.get_machine_stat()
        diff = (self.last_scrape_ts - prev_ts) * nr_hwthreads
        self.pc_user = (self.last_cpu_user - prev_user) / diff
        self.pc_nice = (self.last_cpu_nice - prev_nice) / diff
        self.pc_system = (self.last_cpu_system - prev_system) / diff
        self.pc_idle = (self.last_cpu_idle - prev_idle) / diff
        self.pc_iowait = (self.last_cpu_iowait - prev_iowait) / diff
        self.pc_irq = (self.last_cpu_irq - prev_irq) / diff
        self.pc_softirq = (self.last_cpu_softirq - prev_softirq) / diff
        self.pc_guest = (self.last_cpu_guest - prev_guest) / diff

    def __repr__(self):
        return (f"Host CPU: user: {'%0.02f%%' % self.pc_user}, "
              f"nice: {'%0.02f%%' % self.pc_nice}, "
              f"system: {'%0.02f%%' % self.pc_system}, "
              f"idle: {'%0.02f%%' % self.pc_idle}, "
              f"iowait: {'%0.02f%%' % self.pc_iowait}, "
              f"irq: {'%0.02f%%' % self.pc_irq}, "
              f"guest: {'%0.02f%%' % self.pc_guest}")

    def open_csv_file(self):
        fname = os.path.join(self.args.csv, 'host.csv')
        print("Writing host data in %s" % (fname))
        self.machine_csv = open(fname, 'w')
        self.machine_csv.write("timestamp,cpu_user,cpu_nice,cpu_system,"
                               "cpu_idle,cpu_iowait,cpu_irq,cpu_guest\n")

    def output_machine_csv(self, timestamp):
        self.machine_csv.write(f"{datetime.fromtimestamp(timestamp)},"
                            f"{'%0.02f' % self.pc_user},"
                            f"{'%0.02f' % self.pc_nice},"
                            f"{'%0.02f' % self.pc_system},"
                            f"{'%0.02f' % self.pc_idle},"
                            f"{'%0.02f' % self.pc_iowait},"
                            f"{'%0.02f' % self.pc_irq},"
                            f"{'%0.02f' % self.pc_guest}\n")

    def get_machine_stat(self):
        self.last_scrape_ts = time.time()
        with open('/proc/stat', 'r') as f:
            cpu = f.readline().split()
        self.last_cpu_user = int(cpu[1])
        self.last_cpu_nice = int(cpu[2])
        self.last_cpu_system = int(cpu[3])
        self.last_cpu_idle = int(cpu[4])
        self.last_cpu_iowait = int(cpu[5])
        self.last_cpu_irq = int(cpu[6])
        self.last_cpu_softirq = int(cpu[7])
        self.last_cpu_guest = int(cpu[9])

    def check_cgroup(self):
        # Check if cgroup2 is configured
        cgroup2_path = "/sys/fs/cgroup/cgroup.controllers"
        isfile = os.path.exists(cgroup2_path)
        if isfile:
            return(2)
        else:
            return(1)

    def get_cpuset_mount_point(self):
        with open('/proc/mounts', 'r') as f:
            mounts = f.read().split('\n')
        for m in mounts:
            m = m.split()

            # Avoid scenrio where m is empty
            if m:
                if m[0] == 'cgroup':
                    if 'unified' in m[1]:
                        continue
                    if 'cpuset' in m[3]:
                        self.cpuset_mount_point = m[1]
                        return
                    elif 'cgroup2' in m[2]:
                        self.cpuset_mount_point = m[1]
                        return
        if self.cpuset_mount_point is None:
            print("Cgroup cpuset path not found")


    def refresh_stats(self):
        for node in self.nodes.values():
            node.clear_stats()
        try:
            self.all_vms_lock.acquire()
            for vm in self.all_vms.values():
                vm.refresh_stats()
        finally:
            self.all_vms_lock.release()
        # Normalize by CPU count
        for node in self.nodes.values():
            node.vcpu_sum_pc_util /= node.nr_hwthreads
            node.vcpu_sum_pc_steal /= node.nr_hwthreads
            node.vhost_sum_pc_util /= node.nr_hwthreads
            node.vhost_sum_pc_steal /= node.nr_hwthreads
            node.emulators_sum_pc_util /= node.nr_hwthreads
            node.emulators_sum_pc_steal /= node.nr_hwthreads

        self.refresh_machine_stats()

    def account_vcpus(self):
        tmp = {}
        for node_id in self.nodes.keys():
            tmp[node_id] = 0
        for vm in self.all_vms.values():
            tmp[vm.vcpu_primary_node.id] += len(vm.vcpu_threads.keys())
        for node_id in self.nodes.keys():
            self.nodes[node_id].node_vcpu_threads = tmp[node_id]

    def refresh_vm_allocation(self):
        while True:
            # Sleep 1s between scans
            for i in range(10):
                if self.cancel is True:
                    return
                time.sleep(0.1)
            self.list_vms()
            self.refresh_mem_allocation()
            for vm in self.all_vms.values():
                # If a VM switched memory node
                if vm.new_mem_primary_node is not None:
                    try:
                        self.nodes_lock.acquire()
                        vm.mem_primary_node.vm_mem_allocated -= vm.mem_allocated
                        vm.new_mem_primary_node.vm_mem_allocated += vm.mem_allocated
                        vm.mem_primary_node = vm.new_mem_primary_node
                        vm.new_mem_primary_node = None
                    finally:
                        self.nodes_lock.release()
                # If a VM switched vcpu node
                if vm.new_vcpu_primary_node is not None:
                    try:
                        self.nodes_lock.acquire()
                        del vm.vcpu_primary_node.node_vms[vm.vm_pid]
                        vm.vcpu_primary_node.node_vcpu_threads -= vm.nr_vcpus

                        vm.new_vcpu_primary_node.node_vms[vm.vm_pid] = vm
                        vm.new_vcpu_primary_node.node_vcpu_threads += vm.nr_vcpus
                        vm.vcpu_primary_node = vm.new_vcpu_primary_node
                        vm.new_vcpu_primary_node = None
                    finally:
                        self.nodes_lock.release()
            self.account_vcpus()

    def print_initial_count(self):
        for node in self.nodes.values():
            self.print_node_count(node.id)

    def print_node_count(self, node_id):
        node = self.nodes[node_id]
        node.output_allocation()

    def get_nodes(self, cpuset):
        nodes = []
        fullpath = "%s/%s/cpuset.cpus" % (self.cpuset_mount_point, cpuset)
        if os.path.exists(fullpath):
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
            node = Node(node_id, self.args)
            hwthreads = i.split(':')[1].replace(' ', '')
            _i = mixrange(hwthreads)
            for j in _i:
                node.hwthread_list.append(int(j))
            self.nodes[node_id] = node

    def del_vm(self, pid):
        v = self.all_vms[pid]
        del v.vcpu_primary_node.node_vms[pid]
        try:
            self.all_vms_lock.acquire()
            del self.all_vms[pid]
        finally:
            self.all_vms_lock.release()

    def refresh_mem_allocation(self):
        tmp_vm_used_per_node = {}
        for node in self.nodes.values():
            if self.cancel is True:
                return
            tmp_vm_used_per_node[node.id] = 0
            node.refresh_vm_allocation()

        for vm in self.all_vms.values():
            for node_id in vm.mem_used_per_node.keys():
                tmp_vm_used_per_node[node_id] += vm.mem_used_per_node[node_id]

        for node_id in self.nodes.keys():
            self.nodes[node_id].vm_mem_used = tmp_vm_used_per_node[node_id]

    def list_vms(self, progress=False):
        cmd = ["pgrep", "qemu"]
        try:
            pids = subprocess.check_output(
                    cmd, shell=False).strip().decode("utf-8").split('\n')
        except subprocess.CalledProcessError:
            l = list(self.all_vms.keys())
            for v in l:
                self.del_vm(v)
            return
        except KeyboardInterrupt:
            return
        # to track the VMs that have disappeared since the last scan
        previous_vm_list = list(self.all_vms.keys())
        nr = 0
        for pid in pids:
            if self.cancel is True:
                return
            nr += 1
            if progress is True:
                print("%d VMs" % nr, end='\r')
            pid = int(pid)
            if pid in self.all_vms.keys():
                previous_vm_list.remove(pid)
                continue
            try:
                v = VM(self.args, pid, self)
            except KeyboardInterrupt:
                return
            except:
                print("Unexpected error on VM creation:", sys.exc_info())
                continue
            v.vcpu_primary_node.node_vms[pid] = v
            try:
                self.all_vms_lock.acquire()
                self.all_vms[pid] = v
            finally:
                self.all_vms_lock.release()

        if progress is True:
            print("%d VMs" % nr)
            print("Done")

        if len(previous_vm_list) != 0:
            for pid in previous_vm_list:
                self.del_vm(pid)


class VmTop:
    def __init__(self):
        self.parse_args()
        self.machine = Machine(self.args)
        signal.signal(signal.SIGTERM, self.exit_gracefully)
        print("Collecting VM informations...")
        self.machine.get_info()
        self.vm_alloc_thread = None
        if self.args.csv is not None:
            self.csv = True
            self.open_csv_files()
        else:
            self.csv = False

        # Initial list and allocation accounting
        self.machine.list_vms(progress=True)
        self.machine.refresh_mem_allocation()
        self.machine.account_vcpus()
        if self.csv is False:
            self.machine.print_initial_count()

        # Scan for new VMs and memory/vcpu allocation in the background
        self.vm_alloc_thread = threading.Thread(
                target=self.machine.refresh_vm_allocation)
        self.vm_alloc_thread.start()

        # Main loop
        try:
            self.loop()
        finally:
            # If the main loop crashes, cancel the poller thread
            self.machine.cancel = True

    def exit_gracefully(self, signum, frame):
        self.machine.cancel = True

    def parse_args(self):
        parser = argparse.ArgumentParser(description='Monitor local steal')
        parser.add_argument('-r', '--refresh', type=int, default=1,
                            help='refresh rate (seconds)')
        parser.add_argument('-l', '--limit', type=int,
                            help='limit to top X VMs per node')
        parser.add_argument('-s', '--sort', type=str,
                            choices=['vcpu_util', 'vcpu_steal',
                                     'vhost_util', 'vhost_steal',
                                     'disk_read', 'disk_write',
                                     'emulators_util', 'emulators_steal',
                                     'rx', 'tx', 'rx_dropped', 'tx_dropped'],
                            default='vcpu_util',
                            help='sort order for VM list, default: vcpu_util')
        parser.add_argument('-p', '--pid', type=str,
                            help='Limit to pid (csv), implies --vm')
        parser.add_argument('--vcpu', action='store_true',
                            help='show vcpu stats (implies --vm)')
        parser.add_argument('--no-nic', action='store_true',
                            help='Don\'t collect NIC info')
        parser.add_argument('--csv', type=str,
                            help='Output as CSV files in provided folder name')
        parser.add_argument('--emulators', action='store_true',
                            help='show emulators stats (implies --vm)')
        parser.add_argument('--balance', action='store_true',
                            help='Propose a way to balance load between nodes')
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
        elif self.args.sort == 'vhost_util':
            self.args.sort = 'vhost_sum_pc_util'
        elif self.args.sort == 'vhost_steal':
            self.args.sort = 'vhost_sum_pc_steal'
        elif self.args.sort == 'emulators_util':
            self.args.sort = 'emulators_sum_pc_util'
        elif self.args.sort == 'emulators_steal':
            self.args.sort = 'emulators_sum_pc_steal'
        elif self.args.sort == 'disk_read':
            self.args.sort = 'mb_read'
        elif self.args.sort == 'disk_write':
            self.args.sort = 'mb_write'
        elif self.args.sort == 'rx':
            self.args.sort = 'rx_rate'
        elif self.args.sort == 'tx':
            self.args.sort = 'tx_rate'
        elif self.args.sort == 'rx_dropped':
            self.args.sort = 'rx_rate_dropped'
        elif self.args.sort == 'tx_dropped':
            self.args.sort = 'tx_rate_dropped'

        self.args.vm_format = '{:<19s}{:<8s}{:<8s}{:<8s}{:<8s}{:<8s}{:<8s}{:<8s}{:<8s}{:<8s}{:<8s}{:<8s}{:<8s}{:<8s}'

        # filter by node
        if self.args.node is not None:
            nodes = []
            for n in self.args.node.split(','):
                nodes.append(int(n))
            self.args.node = nodes

    def open_csv_files(self):
        try:
            os.mkdir(self.args.csv)
        except FileExistsError:
            print("Error: folder %s already exists, aborting" % self.args.csv)
            sys.exit(1)
        self.machine.open_csv_file()
        for n in self.machine.nodes.values():
            n.open_csv_file()
        if self.args.vm is True:
            print("Writing per-VM csv file")
        else:
            print("NOT writing per-VM data")

    def check_diskspace(self):
        # Be nice and abort if disk left is less than 1GB
        if self.args.csv is not None:
            if shutil.disk_usage(self.args.csv)[2] < 1*1024*1024*1024:
                print("Less than 1GB available on disk, exiting")
                self.machine.cancel = True
                sys.exit(1)

    def check_balance(self):
        # PoC, lots of assumptions here...
        # Try to optimize the load by proposing a balance between nodes:
        # - find the busiest node
        # - if above a threshold:
        #   - find the busiest VM running there
        #   - find the calmest VM on the other node with the same allocation
        #     - if no VM of the same size is found, use more than one (TODO)
        #   - propose a swap
        if not self.args.balance or self.machine.nr_nodes < 2:
            return

        busiest_node_steal = 0
        busiest_node = None
        calmest_node_steal = None
        calmest_node = None
        # Find the busiest and calmest nodes
        for node in self.machine.nodes.values():
            if node.vcpu_sum_pc_steal > busiest_node_steal:
                busiest_node_steal = node.vcpu_sum_pc_steal
                busiest_node = node
            if calmest_node_steal is None or node.vcpu_sum_pc_steal < calmest_node_steal:
                calmest_node_steal = node.vcpu_sum_pc_steal
                calmest_node = node

        # Not enough steal or capacity
        if busiest_node_steal < 10 or calmest_node_steal > 1:
            return

        # Find the best order to swap (we temporarily need twice the memory
        # capacity on one of the nodes)
        to_calm = None
        if busiest_node.vm_mem_used < calmest_node.vm_mem_used:
            to_calm = False
        else:
            to_calm = True

        busiest_vm = sorted(busiest_node.node_vms.values(),
                            key=operator.attrgetter("vcpu_sum_pc_util"),
                            reverse=True)[0]

        calmest_vm = None
        try:
            self.machine.nodes_lock.acquire()
            for vm in sorted(calmest_node.node_vms.values(),
                            key=operator.attrgetter("vcpu_sum_pc_util"),
                            reverse=False):
                if vm.vcpu_sum_pc_util > 60:
                    break
                if vm.nr_vcpus == busiest_vm.nr_vcpus and \
                        vm.mem_allocated == busiest_vm.mem_allocated:
                    calmest_vm = vm
                    break
        finally:
            self.machine.nodes_lock.release()

        if calmest_vm is None:
            print("Didn't find a candidate to swap with %s (%s): %d vcpus, %m mem" % (
                busiest_vm.name, busiest_vm.vm_pid, busiest_vm.nr_vcpus,
                busiest_vm.mem_allocated))
            return

        if to_calm is True:
            print(f"Would migrate {busiest_vm.name} ({busiest_vm.vm_pid}) "
                  f"{'%0.02f%%' % (busiest_vm.vcpu_sum_pc_util)} from node "
                  f"{busiest_node.id} to node {calmest_node.id}")
            print(f"Would migrate {calmest_vm.name} ({calmest_vm.vm_pid}) "
                  f"{'%0.02f%%' % (calmest_vm.vcpu_sum_pc_util)} from node "
                  f"{calmest_node.id} to node {busiest_node.id}")
        else:
            print(f"Would migrate {calmest_vm.name} ({calmest_vm.vm_pid}) "
                  f"{'%0.02f%%' % (calmest_vm.vcpu_sum_pc_util)} from node "
                  f"{calmest_node.id} to node {busiest_node.id}")
            print(f"Would migrate {busiest_vm.name} ({busiest_vm.vm_pid}) "
                  f"{'%0.02f%%' % (busiest_vm.vcpu_sum_pc_util)} from node "
                  f"{busiest_node.id} to node {calmest_node.id}")

    def loop(self):
        while not self.machine.cancel:
            try:
                time.sleep(self.args.refresh)
            except KeyboardInterrupt:
                if self.vm_alloc_thread is not None:
                    self.machine.cancel = True
                    self.vm_alloc_thread.join()
                return
            self.check_diskspace()
            if not self.vm_alloc_thread.is_alive():
                print("Background allocation thread died, exiting")
                sys.exit(1)
            self.machine.refresh_stats()
            self.check_balance()
            if self.csv is False:
                print("\n%s" % datetime.today())
            else:
                timestamp = int(time.time())
            if self.csv:
                self.machine.output_machine_csv(timestamp)
            else:
                print(self.machine)

            try:
                # Prevent the list of VMs per node to be updated during
                # the output
                self.machine.nodes_lock.acquire()
                for node in self.machine.nodes.keys():
                    if self.args.node is not None:
                        if node not in self.args.node:
                            continue
                    nr = 0
                    node = self.machine.nodes[node]
                    if self.args.vm and self.csv is False:
                        print("Node %d:" % node.id)
                        if not self.args.vcpu:
                            print(self.args.vm_format.format(
                                "Name", "PID", "vcpu", "vcpu",
                                "vhost", "vhost", "emu",
                                "emu", "disk", "disk",
                                "rx", "tx", "rx_drop", "tx_drop"))
                            print(self.args.vm_format.format(
                                "", "", "util%", "steal%",
                                "util%", "steal%", "util%",
                                "steal%", "rd MB/s", "wr MB/s",
                                "Mbps", "Mbps", "pkt/s", "pkt/s"))
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
                                if self.csv is True:
                                    vm.output_vm_csv(timestamp)
                                else:
                                    print(vm)
                                nr += 1
                    if self.csv:
                        node.output_node_csv(timestamp)
                    else:
                        print("  Node %d: vcpu util: %0.02f%%, "
                              "vcpu steal: %0.02f%%, emulators util: %0.02f%%, "
                              "emulators steal: %0.02f%%" % (
                                  node.id,
                                  node.vcpu_sum_pc_util,
                                  node.vcpu_sum_pc_steal,
                                  node.emulators_sum_pc_util,
                                  node.emulators_sum_pc_steal))
                        self.machine.print_node_count(node.id)
            finally:
                self.machine.nodes_lock.release()

    def run(self):
        pass

if os.geteuid() != 0:
    print("Need to run as root")
    sys.exit(1)

s = VmTop()
s.run()
