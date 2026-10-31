# Time synchronization (chrony)

NetRadar nodes must share a common clock so that frames captured "at the same
time" really are simultaneous. The fusion center is the **time authority**
(chrony `master` role) and every radar node syncs to it (`slave` role), with
the internet NTP pool as a fallback.

`chrony-setup.sh` is identical in NetRadar-Node and NetRadar-FusionCenter, so
run it from whichever repository is cloned on that machine.

## 1. Fusion center (time authority)

```bash
sudo ./chrony-setup.sh master
```

It serves the LAN subnet it detects automatically and, if the machine runs
Tailscale, the Tailscale range `100.64.0.0/10`. Pass `--allow <CIDR>` to
choose the client subnets yourself. If a firewall (ufw/firewalld) is active,
the script opens NTP (123/UDP).

## 2. Each radar node

```bash
sudo ./chrony-setup.sh slave <fusion-center-host>
```

`<fusion-center-host>` is the fusion center's Tailscale name (e.g.
`my-fusion-center`) or IP address. After ~30 s, check:

```bash
chronyc sources -v     # the fusion center should be marked '*' (selected)
chronyc tracking       # "System time" shows the current offset
```

`sudo ./chrony-setup.sh verify` prints the same summary.

## 3. Measuring synchronization quality (optional)

Python dependencies (on each node):

```bash
sudo apt-get install -y python3-numpy python3-scipy python3-matplotlib
```

Collect offsets on each node (here: 100 samples, 1 s apart, labelled):

```bash
python3 collect_offsets.py 100 1.0 node1     # on node1
python3 collect_offsets.py 100 1.0 node2     # on node2
```

Results are written to `data/jsons/offset_data_<timestamp>.json` (the label is
stored inside the file). Note the file name each run prints.

Analyze one node, or compare several (after copying each node's file to one
machine and renaming it, e.g. `node1_offsets.json`):

```bash
python3 analyze_offsets.py data/jsons/offset_data_<timestamp>.json --plot
python3 analyze_offsets.py --compare node1_offsets.json node2_offsets.json
```

Worst-case offset between any two devices (copy the JSON files from all nodes
to one machine first):

```bash
python3 worst_case_offset.py node1_offsets.json node2_offsets.json
```

## Quick checks

On the fusion center:

```bash
systemctl status chrony
chronyc clients        # nodes that are syncing
chronyc tracking
```

On a node:

```bash
watch -n 1 chronyc tracking
chronyc sourcestats
sudo systemctl restart chrony
```

## Troubleshooting

- **Node never selects the fusion center:** check that it is reachable
  (`ping <fusion-center-host>`), that the master's allowed subnets include the
  node's address (`chronyc clients` on the master), and that UDP 123 is open.
- **Large initial offset:** `sudo chronyc makestep`.
- **`chrony.service` is masked** (it was disabled in favour of
  systemd-timesyncd): the script fixes this automatically; manually it is
  `sudo systemctl unmask chrony && sudo systemctl disable --now systemd-timesyncd && sudo systemctl enable --now chrony`.
