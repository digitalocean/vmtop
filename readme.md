# KVM trace stats

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
