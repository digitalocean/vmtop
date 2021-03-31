# KVM trace stats

Collection of ad-hoc tools for monitoring various performance aspects of KVM.
The tools are in varied state of maintenance, feel free to reach out if you
have questions or suggestions for improvements.


## vmtop.py

Monitor load and steal for qemu VMs, various filtering capabilities.
It can output as CSV and the `graph-vmtop.py` can generate graphs from those
CSV files.


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
