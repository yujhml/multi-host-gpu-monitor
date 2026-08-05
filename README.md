# multi-host-gpu-monitor

One nvtop-style view across every GPU machine you can ssh to.

`nvtop` shows you one box. This shows the whole fleet in a single pane — every
GPU as its own panel, plus one process table spanning all hosts — over
persistent multiplexed ssh connections, skipping anything that is down or
waiting on authentication.

```
gpu-monitor  6/8 hosts up · 10 GPUs · 7 processes   grid 3x4   14:22:07

gpu-a · Dev 0 [RTX 3090]                 gpu-b · Dev 1 [RTX 2080 Ti]
 PCIe GEN 3@16x  RX 22.2M TX 5.9M         PCIe GEN 1@8x  RX 341.0K TX 439.0K
 1755/9501MHz 46°C 31% 224/350W           300/405MHz 26°C 22% 18/100W
 GPU[|||||||||||||||||||||||     71%]     GPU[                        0%]
 MEM[||||||||||||||  13.2Gi/24.0Gi]       MEM[               0.009Gi/11.0Gi]
 CPU[|||||     31.4%] 32c 412p 1980t 6d2h CPU[|      5.7%] 20c 380p 448t 2d14h

  HOST         PID USER       DEV TYPE  GPU   GPU MEM    CPU  HOST MEM  COMMAND
  gpu-a    3522167 alice        0 C     70%      9.2G  251.7      2.0G  python train.py --config resnet50 --epochs 200~
  gpu-b    2221753 bob          1 C     59%      8.1G  121.7      3.0G  python -m myproject.sweep --seed 4 --batch 256~
```

## Install

No dependencies beyond the Python standard library.

```bash
git clone git@github.com:yujhml/multi-host-gpu-monitor.git
cd multi-host-gpu-monitor
cp hosts.example.txt hosts.txt      # then edit
./gpu-monitor
```

Optionally put it on your PATH:

```bash
ln -s "$PWD/gpu-monitor" ~/.local/bin/gpu-monitor
```

The symlink is fine — the tool resolves its own real path to find `probe.py`
and `hosts.txt`.

## Hosts

`hosts.txt` — one entry per line, `#` for comments:

```
local          # this machine, probed directly without ssh
gpu-a
gpu-b
lab-ws-03
```

Names resolve through `~/.ssh/config`, so `ProxyJump`, `User`, `Port` and
`ControlMaster` settings there apply automatically. Adding a machine is one
line here, plus an ssh config entry if it needs a jump host.

`hosts.txt` is gitignored, since a host list is usually infrastructure-specific.
If it is missing, `hosts.example.txt` is used instead so a fresh clone still
runs.

## Usage

```bash
gpu-monitor                  # live, refreshes every 3s
gpu-monitor -1               # print once and exit (never height-limited)
gpu-monitor --show-skipped   # explain why hosts were hidden
gpu-monitor -d 5 -t 45       # 5s refresh, 45s per-host timeout
gpu-monitor -f other.txt     # a different host list
gpu-monitor -L stack         # one full-width column
gpu-monitor -L 3x5           # explicit grid
gpu-monitor --no-cache       # ignore the failed-host cache, probe everything
gpu-monitor --close          # tear down the persistent ssh masters
```

### Keys

| key | |
|-----|---|
| `↑` / `↓`, or `k` / `j` | scroll the focused section one row |
| `PgUp` / `PgDn`, or `b` / `space` | one page |
| `Home` / `End`, or `g` / `G` | top / bottom |
| `Tab` | move focus between **yours** and **other users** |
| `q` or `Esc` | quit |

Each section scrolls independently, and `▸` marks the one the keys act on.
Scrolling redraws immediately from the last poll rather than waiting for the
next refresh, so it stays responsive even at a long `--delay`.

### Layout

Every **GPU** gets its own panel. The grid shape is derived from the device
count, not the host count, so a 2-GPU box takes two cells:

```
rows    = trunc(sqrt(devices))    # decimals truncated
columns = ceil(devices / rows)
```

27 live devices give `rows = trunc(5.19) = 5`, `columns = ceil(27/5) = 6`. The
status line reports the shape actually used (`grid 5x6`).

Panels fill **vertically first**: reading down the leftmost column gives the
first `rows` devices, then the next column continues. A host's GPUs stay
adjacent, in `hosts.txt` order.

| `-L` | meaning |
|------|---------|
| `auto` | the rule above (default) |
| `stack` | one full-width column, full nvtop one-liner |
| `RxC` | explicit, e.g. `3x5`. Columns win; rows follow from the device count, so more devices wrap onto more rows |
| `Rx` | rows only — columns derived as `ceil(devices / R)` |

Columns are reduced automatically if the terminal cannot give each panel 34
columns, so the grid degrades gracefully rather than shredding every line. A
wide panel (≥76 columns) prints the full nvtop one-liner; a narrow one reflows
PCIe/RX/TX onto its own line and shortens the GPU name
(`NVIDIA GeForce RTX 2080 Ti` → `RTX 2080 Ti`) rather than truncating data away.

Because the shape follows the devices that are *up*, it changes when a host
comes or goes. Pin it with `-L RxC` if you want it stable.

Hosts alternate between two colours so neighbours are easy to separate, in both
the grid and the process table. Parity is taken over the hosts actually
**displayed**, not the order in `hosts.txt` — otherwise a skipped host would put
two neighbours on the same colour. Since a host keeps its colour across all of
its panels, a two-GPU machine shows as an adjacent same-coloured pair. Change
the pair via `HOST_COLORS` near the top of `gpu-monitor`.

### The CPU line

Each panel ends with the state of the machine the GPU is in:

```
 CPU[|||||     31.4%] 32c  412 tasks  1980 thr  up 6d2h     (wide panel)
 CPU[|||    31.4%] 32c 412p 1980t 6d2h                      (narrow panel)
```

Usage is **normalised across all cores**, so 100% means every core is busy, not
one core saturated. It comes from two `/proc/stat` samples bracketing the same
window used for per-process CPU%, so it costs no extra wall clock. Tasks are
processes; threads come from `/proc/loadavg`.

This describes the host, not the device, so on a multi-GPU machine it repeats
in each of that host's panels — deliberately, because equal-height panels are
what keep the grid rectangular.

The process table takes whatever height the grid and footer leave. On a
terminal too short to show every GPU the **grid** is truncated first — the
table is never squeezed below three rows. If the table feels cramped, trading
grid columns for rows (`-L 9x3`) helps more than `-L stack` does.

### Yours vs everyone else

The process table is split in two, with **70% of the available rows given to
your own processes** and 30% to everybody else's:

```
▸ yours (3)
  gpu-a     174786 you          1 C   99%   158.0M  100.0   727.9M  python -m mypkg.train~
  other users (18)  rows 1-6 of 18
  gpu-b    3650571 someone      0 C   81%     9.2G  256.7     2.0G  python scripts/train.py~
```

Ownership is decided **on each host** by the probe, comparing each process
against the account it is running as there. That matters on a cluster where
your ssh user differs from your local username — it cannot be worked out by
comparing against one local name.

Whatever a section cannot fill is handed to the other rather than left blank,
so three processes of yours plus twenty of everyone else's gives 3 + 8 rather
than 8 + 3. If one side is empty its heading disappears entirely. Adjust the
split with `MINE_SHARE` near the top of `gpu-monitor`.

### Speed and timeouts

The first probe of a host also builds its ssh connection; every later one
reuses it. On a 15-host fleet that is the difference between:

```
cold (no ssh masters)   25.9 s
warm                     4.2 s
```

`--timeout` therefore has to cover a *cold* connection, not a warm probe. That
is what the 30s default is sized for, and it is why a shorter one fails so
badly: if the timeout is under the cold-connect cost, the first sweep times
out, every host is cached as failed, and each retry times out in exactly the
same way — the tool can never establish the connections that would make it
fast. The same fleet at `-t 12` shows 2 of 15 hosts.

If your cold connections are slow, measure before raising the timeout further:

```bash
time ssh -o ControlPath=none -o ControlMaster=no somehost true
```

A frequent cause on university and lab clusters is `GSSAPIAuthentication`,
which stalls ~10s per attempt when there is no Kerberos ticket to be found. On
the fleet this was developed against it cost 20s of every 21s cold connect —
`GSSAPIAuthentication no` under `Host *` in `~/.ssh/config` took that to under
a second. It is left to ssh config rather than forced here, since some sites
genuinely authenticate that way.

A dead host costs a full timeout, so failures are remembered in
`~/.cache/gpu-monitor/failed.json` and re-probed only every `--retry-after`
seconds (default 120). The two defaults are chosen together: a poll waits for
all of its hosts, so a down host stalls the display for `--timeout` seconds
once per `--retry-after`. Raising one without the other makes an outage far
more visible than it needs to be.

The cache stores *when* a host failed rather than when to retry it, so changing
`--retry-after` takes effect immediately for hosts already in it. A host that
starts working again drops out on its next probe. `--no-cache` forces a full
sweep.

## How it works

`probe.py` is piped to each host over ssh and executed there, printing a single
JSON line prefixed with a sentinel. Running the probe remotely rather than
issuing many `nvidia-smi` calls means one round trip per host per refresh, and
lets it read `/proc` for what `nvidia-smi` does not know: real user,
instantaneous CPU%, resident memory, and the full command line.

**Persistent sessions.** ControlMaster options are passed explicitly as well as
inherited from ssh config, so a host present in `hosts.txt` but missing from
`~/.ssh/config` still gets a multiplexed connection — including the whole
ProxyJump chain — instead of a fresh handshake every refresh. `--close` tears
them down.

**Silent skipping.** A host that is down, hung, or waiting on authentication
(Tailscale login, missing key, 2FA) is dropped from the display. Three details
make that reliable:

- `ConnectTimeout` bounds only the TCP connect. An ssh sitting at a Tailscale
  "visit this URL" prompt is already connected, so the whole invocation gets a
  hard wall-clock timeout too.
- A stalled ssh behind a `ProxyJump` spawns a process chain, so each probe runs
  in its own session and the entire **process group** is killed on timeout.
  Otherwise the monitor leaks ssh children on every refresh.
- Success requires the sentinel in the output, not an exit code. A login banner
  or an auth prompt on stdout is not a successful probe.

**Bounded remote work.** Killing the local ssh does not reach the far end, so
the probe also limits itself: the collector passes a deadline under its own
timeout, the probe arms a `SIGALRM` bail-out, and `timeout(1)` wraps the remote
interpreter as a backstop in case Python itself wedges.

**CPU%** is sampled from two `/proc/<pid>/stat` reads inside the probe, so it is
instantaneous like nvtop's. `ps -o %cpu` reports an average since process start,
which reads misleadingly high for a job that ran hot and then went idle.

**Multi-GPU jobs.** `nvidia-smi` emits one row per (pid, GPU), so processes are
keyed on that pair. A job spanning several GPUs appears once per device with
that device's memory, instead of collapsing to whichever came last.

## WSL2

On WSL2 the GPU is paravirtualised through `/dev/dxg` and the Windows driver
runs in WDDM mode, where NVML cannot report per-process GPU memory at all — it
returns a "not available" sentinel. Native Linux hosts are unaffected.

So for a WSL host the probe finds GPU processes by their `/dev/dxg` handle
rather than asking NVML. Per-process GPU memory can then only come from inside
each process; if a compatible reporter is running, those values are shown in
cyan. Otherwise that one column reads `-` for WSL hosts only — every other
column works normally.

## Requirements

- `python3` on every host. The probe prefers `/usr/bin/python3`; a conda python
  earlier on `PATH` starts slower and is more fragile.
- `nvidia-smi` on every host. A host with no GPU is skipped.
- `coreutils` `timeout` on remote hosts is used if present, and skipped if not.

Nothing is installed remotely — the probe is piped in on every run, so editing
`probe.py` here updates every host at once. The flip side: a syntax error in
`probe.py` blanks the whole fleet rather than one host, so run `python3
probe.py` locally after editing it.

## Licence

MIT — see [LICENSE](LICENSE).
