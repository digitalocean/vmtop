#!/usr/bin/env python3

# Measure various core scheduling properties (safety and performance-related).
# Requires read access to the CTF trace files.
# Tested on Ubuntu 18.04 with Babeltrace 1.5.5 library.

import sys
import time
import argparse
import operator
import statistics

NSEC_PER_SEC = 1000000000

try:
    from babeltrace import TraceCollection
except ImportError:
    # quick fix for debian-based distros
    sys.path.append("/usr/local/lib/python%d.%d/site-packages" %
                    (sys.version_info.major, sys.version_info.minor))
    from babeltrace import TraceCollection


begin_ts = None
end_ts = None


class Process:
    def __init__(self, pid, details=True):
        self.pid = pid
        self.comm = None
        self.threads = []
        self.total_runtime = 0
        self.show_details = details
        self.lb_issue_durations = []
        self.lb_issue_start = None

        # (comm,tid) -> co-sched time for all the threads in this process
        self.local_neighbors_stats = {}
        # (comm,tid) -> co-sched time for all the idle thread
        self.idle_neighbors_stats = {}
        # (comm,tid) -> co-sched time for all the threads not belonging to this process
        self.foreign_neighbors_stats = {}
        self.unknown_cosched_ns = 0

    def update_or_create(self, stats_dict, key, co_sched_ns):
        if key not in stats_dict.keys():
            stats_dict[key] = 0
        stats_dict[key] += co_sched_ns

    def update_co_sched_stats(self, sibling_proc, sibling_tuple, prev_tuple, co_sched_ns):
        # Only if we know the neighbor process
        if sibling_proc is None:
            self.unknown_cosched_ns += co_sched_ns
            return

        if sibling_proc.pid == 0:
            self.update_or_create(self.idle_neighbors_stats,
                                  sibling_tuple,
                                  co_sched_ns)
        elif self != sibling_proc:
            if self.pid == 0:
                sibling_proc.update_or_create(sibling_proc.idle_neighbors_stats,
                                              prev_tuple,
                                              co_sched_ns)
            else:
                sibling_proc.update_or_create(sibling_proc.foreign_neighbors_stats,
                                              prev_tuple,
                                              co_sched_ns)
            self.update_or_create(self.foreign_neighbors_stats,
                                  sibling_tuple,
                                  co_sched_ns)
        elif self == sibling_proc:
            sibling_proc.update_or_create(sibling_proc.local_neighbors_stats,
                                          prev_tuple,
                                          co_sched_ns)
            self.update_or_create(self.local_neighbors_stats,
                                  sibling_tuple,
                                  co_sched_ns)
        else:
            sibling_tid = sibling_tuple[1]
            print("Missing case %s and %s" % (prev_tuple, sibling_tid))

    def __str__(self):
        tmp_list = ""
        total = 0
        for neighbor in self.local_neighbors_stats.keys():
            total += self.local_neighbors_stats[neighbor]
            if self.show_details:
                tmp_list += "    - %s (%s): %s ns\n" % (neighbor[0],
                                                         neighbor[1],
                                                         self.local_neighbors_stats[neighbor])
        tmp_list += "  - threads: %s\n" % (self.threads)

        if self.total_runtime == 0 or total == 0:
            pc = 0
        else:
            pc = total / self.total_runtime * 100
        co_sched_list = "  - local neighbors (total: %s ns, %0.3f %% of process runtime):\n%s" % (total, pc, tmp_list)

        tmp_list = ""
        total = 0
        for neighbor in self.idle_neighbors_stats.keys():
            total += self.idle_neighbors_stats[neighbor]
            if self.show_details:
                tmp_list += "    - %s (%s): %s ns\n" % (neighbor[0],
                                                         neighbor[1],
                                                         self.idle_neighbors_stats[neighbor])
        if self.total_runtime == 0 or total == 0:
            pc = 0
        else:
            pc = total / self.total_runtime * 100
        co_sched_list += "  - idle neighbors (total: %s ns, %0.3f %% of process runtime):\n%s" % (total, pc, tmp_list)

        tmp_list = ""
        total = 0
        for neighbor in self.foreign_neighbors_stats.keys():
            total += self.foreign_neighbors_stats[neighbor]
            if self.show_details:
                tmp_list += "    - %s (%s): %s ns\n" % (neighbor[0],
                                                         neighbor[1],
                                                         self.foreign_neighbors_stats[neighbor])
        if self.total_runtime == 0 or total == 0:
            pc = 0
        else:
            pc = total / self.total_runtime * 100
        co_sched_list += "  - foreign neighbors (total: %s ns, %0.3f %% of process runtime):\n%s" % (total, pc, tmp_list)

        if self.total_runtime == 0 or self.unknown_cosched_ns == 0:
            pc = 0
        else:
            pc = self.unknown_cosched_ns / self.total_runtime * 100
        co_sched_list += "  - unknown neighbors  (total: %s ns, %0.3f %% of process runtime)\n" % (
                self.unknown_cosched_ns, pc)

        total = 0
        _min = None
        _max = 0

        for period in self.lb_issue_durations:
            total += period
            if _min is None:
                _min = period
            elif period < _min:
                _min = period
            if period > _max:
                _max = period
        if len(self.lb_issue_durations) == 0:
            pc = 0
            co_sched_list += "  - inefficient periods (total: %s ns, %0.3f %% of process runtime)\n" % (total, pc)
        else:
            pc = total / self.total_runtime * 100
            co_sched_list += "  - inefficient periods (total: %s ns, %0.3f %% of process runtime):\n" % (total, pc)
            co_sched_list += "    - number of periods: %s\n" % (len(self.lb_issue_durations))
            co_sched_list += "    - min period duration: %s ns\n" % (_min)
            co_sched_list += "    - max period duration: %s ns\n" % (_max)
            co_sched_list += "    - average period duration: %0.3f ns\n" % (total/len(self.lb_issue_durations))
            if len(self.lb_issue_durations) > 1:
                co_sched_list += "    - stdev: %0.3f\n" % (statistics.stdev(self.lb_issue_durations))

        return("""Process %s (%s):
  - total runtime: %s ns,
%s""" % (self.pid, self.comm, self.total_runtime, co_sched_list))


class TraceParser:
    def __init__(self, trace, topology_filename, display_pids, show_details,
                 merge_vhost, pid_trust):
        self.trace = trace
        # PID to Process
        self.processes = {}
        # TID to Process
        self.tids = {}
        self.tids_last_switch_in_ts = {}
        self.display_pids = display_pids
        self.show_details = show_details
        self.merge_vhost = merge_vhost
        self.pid_trust = pid_trust
        self.vhost_tids = {} # a mapping between vhost processes TIDs and qemu PID

        self.parse_topo(topology_filename)

        # Map betwen cpu_id and (comm, tid)
        self.tuple_by_hwthread = {}
        self.seen_sched_switch_cpu = []
        self.nr_cpus = len(self.siblings.keys())

        # This is the map between one TID and the time it has spent running at
        # the same as the current thread on the other sibling hwthread. When
        # one the two threads is switched out, it updates its own stats and the
        # stats in the thread running on the sibling hwthread.
        # (comm, tid) as the key to distinguish the swapper threads (PID 0).
        self.current_cosched_begin_ts = {}

    def parse_topo(self, filename):
        self.siblings = {}
        with open(filename) as f:
            for line in f:
                id1 = int(line.split(',')[0])
                id2 = int(line.split(',')[1])
                self.siblings[id1] = id2
                self.siblings[id2] = id1

    def ns_to_hour_nsec(self, ns):
        d = time.localtime(ns/NSEC_PER_SEC)
        return "%02d:%02d:%02d.%09d" % (d.tm_hour, d.tm_min, d.tm_sec,
                                        ns % NSEC_PER_SEC)

    def parse(self):
        # iterate over all the events
        count = 0
        self.ready = False
        for event in self.trace.events:
            if not self.ready:
                # Wait to have seen at least one sched_switch per cpu before starting
                if event.name == "sched:sched_switch":
                    next_tuple = (event['next_comm'], event['next_pid'])
                    self.tuple_by_hwthread[event['cpu_id']] = next_tuple
                    self.current_cosched_begin_ts[next_tuple] = event['timestamp']
                    self.tids_last_switch_in_ts[event['next_pid']] = event['timestamp']

                    if event['cpu_id'] not in self.seen_sched_switch_cpu:
                        self.seen_sched_switch_cpu.append(event['cpu_id'])
                    if len(self.seen_sched_switch_cpu) == self.nr_cpus:
                        self.ready = True
                        global begin_ts
                        begin_ts = event['timestamp']
#                        print(begin_ts)
#                        print("Ready: %d (%s events skipped)" % (len(self.seen_sched_switch_cpu), count))
                    continue

            method_name = "handle_%s" % event.name.replace(":", "_").replace("+", "_")
            # call the function to handle each event individually
            if hasattr(TraceParser, method_name):
                func = getattr(TraceParser, method_name)
                func(self, event)
            count += 1
#            if count == 1000000:
#                print(self.ns_to_hour_nsec(event.timestamp))
#                break
        global end_ts
        end_ts = event['timestamp']

#        print("Trace duration: %s ns (%s events)" % (end_ts - begin_ts, count))

        for p in self.processes.keys():
            if len(self.display_pids) > 0 and p not in self.display_pids:
                continue
            print(self.processes[p])


    # Return a Process from a TID, create it if necessary/possible.
    def get_process_tid(self, tid, pid=None, comm=None):
        # Use the procname to consider the vhost-ZZZ process as part
        # of the same "process" as qemu with PID ZZZ. This is to experiment
        # the impact of having the vhost threads as part of the same group/tag
        # as their qemu process.
        if self.merge_vhost:
            if comm is not None and comm.startswith("vhost-"):
                qemu_pid = int(comm.split('-')[1])
                self.vhost_tids[tid] = qemu_pid
                if pid is not None:
                    pid = qemu_pid
            elif tid in self.vhost_tids.keys() and pid is not None:
                pid = self.vhost_tids[tid]

        if tid in self.tids.keys():
            p = self.tids[tid]
            if tid == pid and p.comm is None and comm is not None:
                p.comm = comm
            return p
        if self.pid_trust:
            if pid is None:
                return None
            if pid in self.processes.keys():
                p = self.processes[pid]
            else:
                p = Process(pid, self.show_details)
                self.processes[pid] = p
                self.tids[pid] = p
            self.tids[tid] = p
            if tid not in p.threads:
                if not comm.startswith("vhost-"):
                    p.threads.append(tid)
        else:
            if tid in self.processes.keys():
                p = self.processes[tid]
            else:
                p = Process(tid, self.show_details)
                self.processes[tid] = p
                self.tids[tid] = p
            self.tids[tid] = p
        return p

    def __check_lb_issues(self, tid, now):
        # check if process (all its threads) are currently running in an
        # inefficient configuration (some threads running alone on a core)

        if tid not in self.tids.keys():
            return
        process = self.tids[tid]

        if len(process.threads) == 1:
            return

        # list of hwthreads where a thread of this process is currently running
        hwthread_list = []
        # find all the threads of the process currently on a cpu
        for cpu_id in self.tuple_by_hwthread.keys():
            thread = self.tuple_by_hwthread[cpu_id][1]
            if thread not in process.threads:
                continue
            hwthread_list.append(cpu_id)

        # how many of those hwthread's cores are only running 1 thread of this process
        nr_inefficient = 0
        for hwthread in hwthread_list:
            if self.siblings[hwthread] not in hwthread_list:
                nr_inefficient += 1

        if nr_inefficient > 1:
            # starting an inefficient period
            if process.lb_issue_start is None:
                #print(process.threads, self.tuple_by_hwthread)
                process.lb_issue_start = now
        else:
            # stopping an inefficient period
            if process.lb_issue_start is not None:
                process.lb_issue_durations.append(now - process.lb_issue_start)
                process.lb_issue_start = None


    def handle_sched_sched_switch(self, event):
        timestamp = event.timestamp
        cpu_id = event["cpu_id"]
        perf_tid = event["perf_tid"]
        # prev PID
        perf_pid = event["perf_pid"]
        prev_comm = event["prev_comm"]
        prev_tid = event["prev_pid"]
        prev_prio = event["prev_prio"]
        prev_state = event["prev_state"]
        next_comm = event["next_comm"]
        next_tid = event["next_pid"]

        prev_tuple = (prev_comm, prev_tid)
        next_tuple = (next_comm, next_tid)
        sibling_cpu_id = self.siblings[cpu_id]

        prev_proc = self.get_process_tid(prev_tid, pid=perf_pid, comm=prev_comm)

        sibling_tuple = self.tuple_by_hwthread[sibling_cpu_id]
        sibling_tid = sibling_tuple[1]
        sibling_proc = self.get_process_tid(sibling_tid)

        # SWITCH OUT
        if prev_tuple in self.current_cosched_begin_ts.keys():
            co_sched_ns = timestamp - self.current_cosched_begin_ts[prev_tuple]
            prev_proc.update_co_sched_stats(sibling_proc, sibling_tuple, prev_tuple, co_sched_ns)
        if prev_tid in self.tids_last_switch_in_ts.keys():
            prev_proc.total_runtime += timestamp - self.tids_last_switch_in_ts[prev_tid]

        # SWITCH IN
        self.current_cosched_begin_ts[next_tuple] = timestamp
        self.current_cosched_begin_ts[sibling_tuple] = timestamp
        self.tuple_by_hwthread[cpu_id] = next_tuple
        next_proc = self.get_process_tid(next_tid)
        self.tids_last_switch_in_ts[next_tid] = timestamp

        self.__check_lb_issues(prev_tid, timestamp)
        self.__check_lb_issues(next_tid, timestamp)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Trace parser')
    parser.add_argument('path', metavar="<path/to/trace>", help='Trace path')
    parser.add_argument('--topology', type=str,
                        help='text file obtained with for i in /sys/devices/system/cpu/cpu*/topology/thread_siblings_list; do cat $i; done > out.txt',
                        required=True)
    parser.add_argument('--pids', type=str, help='Only output this coma-separated list of PIDs')
    parser.add_argument('--show-details', action="store_true", help='Breakdown the co-scheduling stats by neighbor process')
    parser.add_argument('--merge-vhost', action="store_true", help='Consider vhost processes as part of the Qemu process (based on the procname)', default=False)
    parser.add_argument('--no-pid-trust', action="store_true", help='Consider vhost processes as part of the Qemu process (based on the procname)', default=False)

    args = parser.parse_args()

    if args.no_pid_trust:
        pid_trust = False
    else:
        pid_trust = True

    traces = TraceCollection()
    handle = traces.add_traces_recursive(args.path, "ctf")
    if handle is None:
        sys.exit(1)

    display_pids = []
    if args.pids is not None:
        for i in args.pids.split(','):
            display_pids.append(int(i))

    t = TraceParser(traces, args.topology, display_pids, args.show_details,
                    args.merge_vhost, pid_trust)
    t.parse()

    for h in handle.values():
        traces.remove_trace(h)
