#!/usr/bin/env python3
"""
gpu-monitor remote probe.

This script is piped to each host over ssh and run there; it prints one line
to stdout:

    @@GPUMON1@@{"host_kind": ..., "gpus": [...], "procs": [...]}

The sentinel matters. A host mid-authentication (tailscale login prompt,
key rejection) or with a chatty MOTD will happily emit text on stdout while
being useless, so the collector requires the sentinel rather than trusting
an exit code.

Everything degrades rather than fails: a missing `pmon`, an unreadable
/proc entry, or a driver that will not report per-process memory each blanks
its own field instead of losing the host.

Two environments are supported:

  * A normal Linux box with the NVIDIA driver, where nvidia-smi reports
    per-process GPU memory directly.
  * WSL2, where the GPU is paravirtualised through /dev/dxg and the host
    driver runs in WDDM mode. There NVML returns a "not available" sentinel
    for per-process memory, so processes are found by scanning /proc for
    /dev/dxg handles and their GPU memory is read from wslnvtop's reports
    if that reporter is running.
"""

import glob
import json
import os
import pwd
import signal
import subprocess
import sys
import time

SENTINEL = "@@GPUMON1@@"
CPU_WINDOW = float(os.environ.get("GPUMON_CPU_WINDOW", "0.6"))

# Hard self-limit, set by the collector to something under its own timeout.
# Without it the probe's per-command budgets can sum to far more than the
# collector allows: the collector would give up first, and killing its local
# ssh does not reach this process, leaving it running on the far host.
DEADLINE = float(os.environ.get("GPUMON_DEADLINE", "0"))
# Per-command budgets are carved out of the deadline so their sum cannot
# outlast it. The fractions leave room for the CPU sampling window.
_B = (DEADLINE - CPU_WINDOW - 0.5) if DEADLINE > 0 else 44.0
T_GPUS = max(1.5, _B * 0.30)
T_PCIE = max(1.5, _B * 0.30)
T_APPS = max(1.5, _B * 0.25)
T_PMON = max(1.0, _B * 0.15)


def arm_deadline():
    """Emit a well-formed timeout payload and exit rather than overrun."""
    if DEADLINE <= 0 or not hasattr(signal, "SIGALRM"):
        return

    def bail(_sig, _frm):
        try:
            sys.stdout.write(SENTINEL + json.dumps({"error": "probe-timeout"}) + "\n")
            sys.stdout.flush()
        finally:
            os._exit(0)

    signal.signal(signal.SIGALRM, bail)
    signal.setitimer(signal.ITIMER_REAL, DEADLINE)
CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
NVML_NOT_AVAILABLE = 2**64 - 1


def sh(cmd, timeout):
    """Run a command, returning stdout, or '' on any failure."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout if p.returncode == 0 else ""


def num(value, cast=float):
    try:
        v = cast(value)
    except (TypeError, ValueError):
        return None
    return v


# ---------------------------------------------------------------------------
# GPUs
# ---------------------------------------------------------------------------

GPU_FIELDS = [
    "index", "name", "uuid", "pcie.link.gen.current", "pcie.link.width.current",
    "clocks.gr", "clocks.mem", "temperature.gpu", "fan.speed", "power.draw",
    "power.limit", "memory.used", "memory.total", "utilization.gpu",
]


def query_gpus():
    out = sh(["nvidia-smi", "--query-gpu=" + ",".join(GPU_FIELDS),
              "--format=csv,noheader,nounits"], T_GPUS)
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(GPU_FIELDS):
            continue
        gpus.append({
            "index": num(parts[0], int), "name": parts[1], "uuid": parts[2],
            "pcie_gen": num(parts[3], int), "pcie_width": num(parts[4], int),
            "clk_gr": num(parts[5], int), "clk_mem": num(parts[6], int),
            "temp": num(parts[7], int), "fan": num(parts[8], int),
            "power": num(parts[9]), "power_cap": num(parts[10]),
            "mem_used": num(parts[11], int), "mem_total": num(parts[12], int),
            "util": num(parts[13], int),
            "rx": None, "tx": None,
        })
    return gpus


def add_pcie_throughput(gpus):
    """Parse Tx/Rx throughput out of `nvidia-smi -q`.

    The values live in each GPU's PCI section and are not exposed by
    --query-gpu at all. They must be attributed per GPU block: a flat grep
    over a multi-GPU host interleaves the values and only lines up by luck.
    """
    out = sh(["nvidia-smi", "-q"], T_PCIE)
    if not out:
        return
    idx = -1
    for line in out.splitlines():
        stripped = line.strip()
        # Section headers look like "GPU 00000000:01:00.0" and sit at column 0.
        # Anchor on the raw line: indented fields such as "GPU Instance ID : N/A"
        # also begin with "GPU " and would otherwise advance the index.
        if line.startswith("GPU ") and ":" in line:
            idx += 1
            continue
        if idx < 0 or idx >= len(gpus):
            continue
        if stripped.startswith("Tx Throughput") or stripped.startswith("Rx Throughput"):
            key = "tx" if stripped.startswith("Tx") else "rx"
            value = stripped.split(":", 1)[1].strip().split()[0]
            kbps = num(value)
            # nvidia-smi labels this KB/s; nvtop displays the same number as
            # KiB/s. Scale by 1024 so the UI's binary formatter reproduces
            # nvtop's reading exactly rather than differing by 2.4%.
            gpus[idx][key] = kbps * 1024 if kbps is not None else None


# ---------------------------------------------------------------------------
# GPU processes -- normal Linux path
# ---------------------------------------------------------------------------

def query_compute_apps(uuid_to_index):
    """{pid: {"dev": idx, "gpu_mem": bytes|None, "type": "C"}}"""
    out = sh(["nvidia-smi",
              "--query-compute-apps=pid,gpu_uuid,used_memory",
              "--format=csv,noheader,nounits"], T_APPS)
    procs = {}
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            continue
        pid = num(parts[0], int)
        if pid is None:
            continue
        mib = num(parts[2], int)
        if mib is not None and mib >= NVML_NOT_AVAILABLE // 2**20:
            mib = None  # WDDM "not available" sentinel
        dev = uuid_to_index.get(parts[1])
        # nvidia-smi emits one row per (pid, gpu): a job spanning several GPUs
        # appears once per device. Keying on pid alone would collapse it to the
        # last device and throw away the other devices' memory.
        procs[(pid, dev)] = {
            "dev": dev,
            "gpu_mem": mib * 2**20 if mib is not None else None,
            "type": "C",
        }
    return procs


def query_pmon(procs):
    """Best-effort per-process SM%% and C/G type via `nvidia-smi pmon`.

    pmon is slow and sometimes returns nothing but dashes, so it gets its own
    short timeout: a stalled pmon must blank two columns, not drop the host.
    """
    out = sh(["nvidia-smi", "pmon", "-c", "1"], T_PMON)
    for line in out.splitlines():
        if line.startswith("#"):
            continue
        f = line.split()
        if len(f) < 5 or f[1] == "-":
            continue
        pid = num(f[1], int)
        if pid is None:
            continue
        dev = num(f[0], int)
        entry = procs.setdefault((pid, dev), {"dev": dev, "gpu_mem": None})
        if entry.get("dev") is None:
            entry["dev"] = dev
        if f[2] != "-":
            entry["type"] = f[2]
        entry["sm"] = num(f[3], int)


# ---------------------------------------------------------------------------
# GPU processes -- WSL2 path
# ---------------------------------------------------------------------------

def is_wsl():
    return os.path.exists("/dev/dxg") and not glob.glob("/dev/nvidia[0-9]*")


def wsl_gpu_procs():
    """Find GPU processes by their /dev/dxg handle, since NVML cannot here."""
    procs = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        holds = False
        for fd in fds:
            try:
                if os.readlink(f"{fd_dir}/{fd}") == "/dev/dxg":
                    holds = True
                    break
            except OSError:
                continue
        if not holds:
            continue
        try:
            with open(f"/proc/{pid}/maps", "rb") as fh:
                cuda = b"libcuda.so" in fh.read()
        except OSError:
            cuda = False
        procs[(pid, 0)] = {"dev": 0, "gpu_mem": None, "type": "C" if cuda else "G"}
    return procs


def wsl_report_dirs():
    tmp = "/tmp"
    cands = []
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        cands.append(os.path.join(base, "wslnvtop"))
    cands.extend(glob.glob("/run/user/*/wslnvtop"))
    cands.extend(glob.glob(os.path.join(tmp, "wslnvtop-*", "wslnvtop")))
    return [d for d in cands if os.path.isdir(d)]


def apply_wsl_reports(procs):
    """Fill GPU memory from wslnvtop's self-reporting processes, if present."""
    now = time.time()
    for d in wsl_report_dirs():
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".json"):
                continue
            # A single malformed report file must not take down the host's
            # whole entry, so this catches broadly and validates shapes rather
            # than trusting the schema.
            try:
                with open(os.path.join(d, name)) as fh:
                    data = json.load(fh)
                if not isinstance(data, dict):
                    continue
                pid = int(data["pid"])
                interval = float(data.get("interval", 1.0))
                if now - float(data.get("ts", 0)) > max(10.0, 3 * interval):
                    continue
                devices = data.get("devices")
                if not isinstance(devices, dict):
                    continue
                reserved = sum(int(v.get("reserved", 0))
                               for v in devices.values() if isinstance(v, dict))
            except Exception:
                continue
            if (pid, 0) in procs:
                procs[(pid, 0)]["gpu_mem"] = reserved
                procs[(pid, 0)]["self_reported"] = True


# ---------------------------------------------------------------------------
# /proc enrichment: user, cpu%, host memory, command
# ---------------------------------------------------------------------------

def proc_stat(pid):
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read().decode("utf-8", "replace")
        rest = raw[raw.rindex(")") + 2:].split()
        return int(rest[11]) + int(rest[12]), int(rest[19])
    except (OSError, ValueError, IndexError):
        return None, None


def enrich(procs):
    """Attach user/cpu/rss/command. CPU%% is sampled over a real interval.

    `ps -o %cpu` would be average-since-start, which reads high for a job that
    ran hot and then went idle. nvtop shows instantaneous usage, so take two
    samples here -- inside the probe -- and return the delta. One round trip.
    """
    pids = {pid for pid, _dev in procs}
    first = {pid: proc_stat(pid) for pid in pids}
    time.sleep(CPU_WINDOW)
    second = {pid: proc_stat(pid) for pid in pids}

    out = []
    for (pid, _dev), info in procs.items():
        ticks0, start0 = first.get(pid, (None, None))
        ticks1, start1 = second.get(pid, (None, None))
        if ticks1 is None or start1 is None:
            continue  # exited during sampling
        cpu = 0.0
        if ticks0 is not None and start0 == start1:
            cpu = 100.0 * ((ticks1 - ticks0) / CLK_TCK) / CPU_WINDOW

        try:
            with open(f"/proc/{pid}/statm") as fh:
                rss = int(fh.read().split()[1]) * PAGE_SIZE
        except (OSError, ValueError, IndexError):
            rss = 0

        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
            cmd = " ".join(cmd.split())
            if not cmd:
                with open(f"/proc/{pid}/comm") as fh:
                    cmd = f"[{fh.read().strip()}]"
        except OSError:
            cmd = "?"

        try:
            uid = os.stat(f"/proc/{pid}").st_uid
            user = pwd.getpwuid(uid).pw_name
        except (OSError, KeyError):
            user = "?"

        out.append({
            "pid": pid, "user": user, "dev": info.get("dev"),
            "type": info.get("type", "C"), "sm": info.get("sm"),
            "gpu_mem": info.get("gpu_mem"), "cpu": cpu, "rss": rss,
            "cmd": cmd, "self_reported": info.get("self_reported", False),
        })
    return out


def main():
    arm_deadline()
    gpus = query_gpus()
    if not gpus:
        # No GPU, or no usable driver -- the collector treats this as a skip.
        print(SENTINEL + json.dumps({"error": "no-gpu"}))
        return 0
    add_pcie_throughput(gpus)

    if is_wsl():
        kind = "wsl"
        procs = wsl_gpu_procs()
        apply_wsl_reports(procs)
    else:
        kind = "linux"
        uuid_to_index = {g["uuid"]: g["index"] for g in gpus}
        procs = query_compute_apps(uuid_to_index)
        query_pmon(procs)

    payload = {
        "host_kind": kind,
        "ts": time.time(),
        "gpus": [{k: v for k, v in g.items() if k != "uuid"} for g in gpus],
        "procs": enrich(procs),
    }
    print(SENTINEL + json.dumps(payload))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never hang or traceback into the collector
        print(SENTINEL + json.dumps({"error": f"probe-failed: {exc}"}))
        sys.exit(0)
