# KVM trace stats

Collection of ad-hoc tools for monitoring various performance aspects of KVM.
The tools are in various states of maintenance, feel free to reach out if you
have questions or suggestions for improvements.


## vmtop.py

Monitor load and steal for QEMU VMs, various filtering capabilities.
It can output as CSV and the `graph-vmtop.py` can generate graphs from those
CSV files (record with `--csv <dir>`). Depends on `numastat` being in the `$PATH`.

Example output for the top 10 VMs experiencing the most vcpu steal per node:

```
$ sudo ./vmtop.py --vm --limit 10 --sort vcpu_steal
[...]
Node 0:
Name     PID     vcpu util   vcpu steal  vhost util  vhost steal emu util  emu steal disk read    disk write   rx           tx
Guest01  48642   141.73 %    32.86 %     0.30 %      0.06 %      0.57 %    0.48 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
Guest02  16830   219.07 %    29.74 %     28.45 %     22.13 %     0.54 %    1.30 %    0.00 MB/s    0.01 MB/s    2.49 MB/s    2.43 MB/s
Guest03  17152   119.61 %    27.87 %     13.89 %     16.31 %     0.48 %    0.12 %    0.00 MB/s    0.00 MB/s    1.45 MB/s    1.39 MB/s
Guest04  60782   52.90 %     16.70 %     0.79 %      0.75 %      0.61 %    0.19 %    0.00 MB/s    0.01 MB/s    0.02 MB/s    0.03 MB/s
Guest05  33077   46.47 %     12.69 %     2.02 %      1.82 %      3.11 %    0.97 %    0.00 MB/s    2.06 MB/s    0.24 MB/s    0.14 MB/s
Guest06  39435   41.70 %     10.01 %     17.36 %     12.85 %     0.98 %    0.08 %    0.00 MB/s    0.00 MB/s    0.27 MB/s    0.30 MB/s
Guest07  5196    26.49 %     9.28 %      0.03 %      0.00 %      0.51 %    0.09 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
Guest08  56822   55.63 %     8.77 %      9.33 %      5.61 %      0.53 %    0.17 %    0.00 MB/s    0.00 MB/s    0.30 MB/s    0.30 MB/s
Guest09  65751   36.59 %     8.54 %      6.26 %      3.42 %      0.76 %    0.06 %    0.08 MB/s    0.04 MB/s    0.18 MB/s    0.18 MB/s
Guest10  25320   26.61 %     8.44 %      0.06 %      0.00 %      5.30 %    2.72 %    0.00 MB/s    0.06 MB/s    0.00 MB/s    0.00 MB/s
  Node 0: vcpu util: 80.85%, vcpu steal: 6.49%, emulators util: 1.71%, emulators steal: 0.89%
  Node 0: 79 VMs (168 vcpus, 306.00 GB mem allocated, 231.98 GB mem used)
Node 1:
Name     PID     vcpu util   vcpu steal  vhost util  vhost steal emu util  emu steal disk read    disk write   rx           tx
Guest11  14506   29.49 %     0.70 %      0.00 %      0.00 %      0.45 %    0.01 %    0.00 MB/s    0.01 MB/s    0.00 MB/s    0.00 MB/s
Guest12  52580   28.69 %     0.63 %      4.55 %      0.26 %      0.60 %    0.08 %    0.12 MB/s    0.00 MB/s    0.16 MB/s    0.15 MB/s
Guest13  60864   36.45 %     0.56 %      0.06 %      0.00 %      4.25 %    0.06 %    0.00 MB/s    0.03 MB/s    0.00 MB/s    0.00 MB/s
Guest14  69426   64.98 %     0.54 %      11.06 %     0.79 %      0.09 %    0.00 %    0.00 MB/s    0.02 MB/s    4.20 MB/s    3.87 MB/s
Guest15  52138   52.45 %     0.39 %      10.66 %     0.64 %      0.75 %    0.02 %    0.22 MB/s    0.00 MB/s    0.43 MB/s    0.38 MB/s
Guest16  38308   57.05 %     0.25 %      12.03 %     0.93 %      1.18 %    0.03 %    0.51 MB/s    0.00 MB/s    0.47 MB/s    0.56 MB/s
Guest17  7700    11.87 %     0.25 %      0.08 %      0.00 %      4.16 %    0.05 %    0.00 MB/s    0.03 MB/s    0.00 MB/s    0.00 MB/s
Guest18  39542   39.46 %     0.24 %      7.61 %      0.51 %      0.77 %    0.02 %    0.16 MB/s    0.00 MB/s    0.27 MB/s    0.27 MB/s
Guest19  1381    49.03 %     0.24 %      9.01 %      0.72 %      0.76 %    0.05 %    0.25 MB/s    0.00 MB/s    0.35 MB/s    0.43 MB/s
Guest20  6945    36.15 %     0.24 %      6.42 %      0.45 %      0.77 %    0.04 %    0.21 MB/s    0.00 MB/s    0.23 MB/s    0.36 MB/s
  Node 1: vcpu util: 41.88%, vcpu steal: 0.20%, emulators util: 1.87%, emulators steal: 0.08%
  Node 1: 131 VMs (161 vcpus, 210.00 GB mem allocated, 193.42 GB mem used)
```

Assumption: the node-level information assumes a VM mostly runs where most of
its memory is allocated, if a VM can float between NUMA nodes, the node-level
information may not be accurate (and a warning will be shown). The VM-level
data is accurate regardless of the pinning.

This script can also be ran with a Prometheus exporter enabled, like so:
```
sudo ./vmtop.py --prometheus [host:port]
```

The host and port are optional, and will default to localhost:8000 if not specified. 

## guesttime.bt

`bpftrace` tool to check statistically how long a vCPU spends inside the guest
when it is scheduled in.


## Core-scheduling and KVM

The `core-sched-stats.py` script ensures that the core scheduling
feature works properly and accounts for the time spent by a vCPU in various
scheduling modes (co-scheduled with idle, with a compatible task, or a foreign
task).

This script works with perf trace recorded:
```
sudo perf record -e kvm:kvm_entry -e kvm:kvm_exit -e sched:sched_switch -e sched:sched_waking -o perf.data -a sleep 60
```

and can be converted to CTF like so (requires perf compiled with CTF support):
```
perf data convert --to-ctf=./ctf -i perf.data
```

If you see this message and the number of chunks is greater than 1 or 2, consider writing your `perf.data` to a ramdisk instead
of your local disk:
```
Processed 7378132 events and lost 1 chunks!
Check IO/CPU overload!
```

It depends on Babeltrace compiled with the python library and Perf compiled with the CTF support. On bionic:

```
apt-get install libbabeltrace-dev libbabeltrace-ctf-dev python3-babeltrace babeltrace
```

Then rebuild `perf` so it will detect the new library.

In order to run `kvm_co_sched_stats.py`, the siblings list must be provided in a text file with the `--topology` flag.
To collect this data, run this on the target HV:
```
for i in /sys/devices/system/cpu/cpu*/topology/thread_siblings_list; do cat $i; done > out.txt
```
