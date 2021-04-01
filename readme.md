# KVM trace stats

Collection of ad-hoc tools for monitoring various performance aspects of KVM.
The tools are in varied state of maintenance, feel free to reach out if you
have questions or suggestions for improvements.


## vmtop.py

Monitor load and steal for qemu VMs, various filtering capabilities.
It can output as CSV and the `graph-vmtop.py` can generate graphs from those
CSV files (record with `--csv <dir>`).

Example output for the top 10 VMs experiencing the most vcpu steal per node:

```
2021-04-01 13:40:28.339481
Node 0:
Name               PID     vcpu util   vcpu steal  vhost util  vhost steal emu util  emu steal disk read    disk write   rx           tx
Guest9856    48157   4.33 %      6.48 %      0.00 %      0.00 %      0.57 %    0.16 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
Guest9802    3874    35.66 %     4.47 %      0.00 %      0.00 %      0.51 %    0.36 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
Guest9781    71011   4.15 %      4.18 %      0.01 %      0.00 %      0.54 %    0.23 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
Guest9850    39370   32.06 %     4.00 %      0.00 %      0.00 %      0.47 %    0.49 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
Guest9858    50607   67.71 %     3.08 %      0.08 %      0.14 %      0.51 %    0.85 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
Guest9806    6590    2.25 %      3.05 %      0.00 %      0.00 %      0.54 %    0.37 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
Guest9859    51877   2.42 %      2.55 %      0.00 %      0.00 %      0.48 %    0.35 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
Guest0538    38324   4.26 %      2.47 %      0.00 %      0.00 %      0.53 %    0.57 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
Guest0503    12159   2.09 %      2.43 %      0.00 %      0.00 %      0.53 %    0.39 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
Guest9845    35740   1.93 %      2.41 %      0.00 %      0.00 %      0.49 %    0.33 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
  Node 0: vcpu util: 82.20%, vcpu steal: 4.01%, emulators util: 2.60%, emulators steal: 1.74%
  Node 0: 198 VMs (198 vcpus, 198.00 GB mem allocated, 188.24 GB mem used)

Node 1:
Name               PID     vcpu util   vcpu steal  vhost util  vhost steal emu util  emu steal disk read    disk write   rx           tx
Guest0899    77310   49.58 %     0.03 %      0.00 %      0.00 %      0.28 %    0.00 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
Guest4253    64301   46.52 %     0.02 %      0.00 %      0.00 %      0.41 %    0.00 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
Guest4664    60847   1.14 %      0.02 %      0.00 %      0.00 %      0.42 %    0.00 %    0.00 MB/s    0.01 MB/s    0.00 MB/s    0.00 MB/s
Guest0532    34125   46.83 %     0.01 %      0.00 %      0.00 %      0.39 %    0.00 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
Guest0913    20530   43.79 %     0.01 %      0.04 %      0.00 %      0.37 %    0.01 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
Guest0490    2654    1.14 %      0.01 %      0.00 %      0.00 %      0.37 %    0.01 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
Guest0906    8821    0.53 %      0.01 %      0.00 %      0.00 %      0.29 %    0.00 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
Guest0510    17978   44.93 %     0.01 %      0.00 %      0.00 %      0.41 %    0.02 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
Guest0516    21240   0.89 %      0.01 %      0.00 %      0.00 %      0.36 %    0.02 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
Guest9892    72659   0.79 %      0.01 %      0.00 %      0.00 %      0.35 %    0.00 %    0.00 MB/s    0.00 MB/s    0.00 MB/s    0.00 MB/s
  Node 1: vcpu util: 21.54%, vcpu steal: 0.00%, emulators util: 0.77%, emulators steal: 0.02%
  Node 1: 87 VMs (87 vcpus, 87.00 GB mem allocated, 85.09 GB mem used)
```


## guesttime.bt

`bpftrace` tool to check statistically how long a vCPU spends inside the guest
when it is scheduled in.


## Core-scheduling and KVM

The `core-sched-stats.py` script allows to ensure that the core scheduling
feature works properly and account the time spent by a vCPU in various
scheduling mode (co-scheduled with idle, with a compatible task or a foreign
task).

Those script work with perf trace recorded like that:
```
sudo perf record -e kvm:kvm_entry -e kvm:kvm_exit -e sched:sched_switch -e sched:sched_waking -o perf.data -a sleep 60
```

and converted to CTF like that (requires perf compiled with CTF support):
```
perf data convert --to-ctf=./ctf -i perf.data
```

If you see this message and the number of chunks is greater than 1 or 2, consider writing your `perf.data` in a ramdisk instead
of your local disk:
```
Processed 7378132 events and lost 1 chunks!
Check IO/CPU overload!
```

They depend on Babeltrace compiled with the python library and Perf compiled with the CTF support. On bionic:

```
apt-get install libbabeltrace-dev libbabeltrace-ctf-dev python3-babeltrace babeltrace
```

And rebuild `perf` so it will detect the new library.

In order to run `kvm_co_sched_stats.py`, the siblings list must be provided in a text file with the `--topology` flag.
To collect this data, run this on the target HV:
```
for i in /sys/devices/system/cpu/cpu*/topology/thread_siblings_list; do cat $i; done > out.txt
```
