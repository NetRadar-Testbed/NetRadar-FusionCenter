#!/usr/bin/env bash
# =============================================================================
# chrony-setup.sh — Chrony NTP setup for a NetRadar network
#
# Topology: the FUSION CENTER (or any designated server) is the NTP authority.
# Run the 'master' role there — it syncs to internet NTP and serves the LAN.
# Every other machine runs the 'slave' role: it syncs to the fusion center as
# its PREFERRED source, falls back to the internet NTP pool if the fusion
# center is unreachable, and does not serve time itself. Any server can host
# the 'master' role; only one authority should exist per network.
#
# USAGE
#   sudo ./chrony-setup.sh master [--allow <CIDR> ...] [--upstream <pool> ...]
#   sudo ./chrony-setup.sh slave  <FUSION_CENTER_IP> [--server <IP> ...] \
#                                [--upstream <pool> ...]
#   sudo ./chrony-setup.sh verify
#
# EXAMPLES
#   # Fusion center as the NTP authority, serving the LAN subnet it sits on
#   # (auto-detected):
#   sudo ./chrony-setup.sh master
#
#   # Fusion center, explicit client subnet:
#   sudo ./chrony-setup.sh master --allow 10.0.0.0/24
#
#   # Jetson node syncing to the fusion center (IP or Tailscale DNS name),
#   # with a secondary LAN server as a non-preferred fallback:
#   sudo ./chrony-setup.sh slave my-fusion-center --server 192.0.2.10
#
# Idempotent: re-running rewrites the same config, keeping a timestamped backup
# of the previous one in /etc/chrony/backups/. No interactive prompts, so it is
# safe to run from Ansible / ssh one-liners.
# =============================================================================

set -euo pipefail

LOG_PREFIX="[chrony-setup]"

# Overridable upstream pools. If empty, the design-doc defaults are used.
UPSTREAMS=()
ALLOW_CIDRS=()
# Secondary LAN NTP servers (slave role only), after the fusion center.
SERVERS=()
ROLE=""
MASTER_IP=""

log()  { printf '%s %s\n' "$LOG_PREFIX" "$*"; }
die()  { printf '%s ERROR: %s\n' "$LOG_PREFIX" "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
chrony-setup.sh — Chrony NTP setup for NetRadar nodes

Topology: the fusion center is the NTP authority; every other machine is a
client of it and falls back to the internet NTP pool.

USAGE
  sudo ./chrony-setup.sh master [--allow <CIDR> ...] [--upstream <pool> ...]
  sudo ./chrony-setup.sh slave  <FUSION_CENTER_IP> [--server <IP> ...] \
                               [--upstream <pool> ...]
  sudo ./chrony-setup.sh verify

ROLES
  master   The NTP authority — run this on the fusion center (any server can
           host; only one per network). Syncs to internet NTP pools and serves
           the LAN on 123/UDP.
  slave    A radar node. Syncs to the fusion center as the PREFERRED source
           (trust prefer, NOT require, so the internet pool can take over if
           the fusion center is unreachable). Does not serve time itself.
           Pass the fusion center's IP or Tailscale name.
  verify   Report sync status without changing anything.

OPTIONS
  --allow <CIDR>      Client subnet the master serves. Repeatable; defaults to
                      the LAN CIDR of the node's default-route interface
                      plus the Tailscale range 100.64.0.0/10 when this
                      machine runs Tailscale. Master role only.
  --server <IP>       Secondary LAN NTP server (non-preferred fallback, listed
                      after the fusion center). Repeatable. Slave role only.
  --upstream <pool>   Internet NTP pool to use instead of the defaults
                      (time.google.com, ntp.ubuntu.com). Repeatable.
  -h, --help          Show this help.
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
parse_args() {
  local positional=()
  while (($#)); do
    case "$1" in
      --allow)
        (($# >= 2)) || die "--allow requires a CIDR (e.g. 192.168.1.0/24)"
        ALLOW_CIDRS+=("$2"); shift 2 ;;
      --upstream)
        (($# >= 2)) || die "--upstream requires a hostname (e.g. time.google.com)"
        UPSTREAMS+=("$2"); shift 2 ;;
      --server)
        (($# >= 2)) || die "--server requires an IP or hostname"
        SERVERS+=("$2"); shift 2 ;;
      -h|--help) usage; exit 0 ;;
      --) shift; positional+=("$@"); break ;;
      -*) die "unknown option: $1 (see --help)" ;;
      *) positional+=("$1"); shift ;;
    esac
  done
  ROLE="${positional[0]:-}"
  MASTER_IP="${positional[1]:-}"
}

# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
ensure_installed() {
  local missing=() pkg
  for pkg in chrony fake-hwclock; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
  done
  if ((${#missing[@]})); then
    log "Installing: ${missing[*]}"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y --no-install-recommends "${missing[@]}"
  else
    log "chrony and fake-hwclock already installed"
  fi
  # The Jetson has no battery-backed RTC; fake-hwclock saves/restores time
  # across reboots so chrony re-converges fast (design doc §2.3).
  systemctl enable fake-hwclock >/dev/null 2>&1 || true
}

backup_config() {
  local conf=/etc/chrony/chrony.conf bak_dir=/etc/chrony/backups
  [[ -f $conf ]] || return 0
  mkdir -p "$bak_dir"
  local bak="$bak_dir/chrony.conf.bak.$(date +%Y%m%d-%H%M%S)"
  cp -a "$conf" "$bak"
  log "Backed up previous config -> $bak"
}

detect_lan_cidr() {
  # CIDR of the interface used by the default route (the LAN the Orins share).
  local iface
  iface=$(ip route get 8.8.8.8 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="dev") {print $(i+1); exit}}')
  [[ -n $iface ]] || return 1
  ip -o -4 addr show dev "$iface" scope global 2>/dev/null | awk '{print $4; exit}'
}

write_master_conf() {
  local conf=/etc/chrony/chrony.conf
  log "Writing master config -> $conf"

  # Design-doc defaults (§2.2) unless --upstream overrides them.
  if ((${#UPSTREAMS[@]})); then
    local pools=("${UPSTREAMS[@]}")
  else
    local pools=("time.google.com" "ntp.ubuntu.com")
  fi

  {
    echo "# Chrony MASTER config — generated by chrony-setup.sh ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
    echo "# NetRadar network: this node (the fusion center) is the time authority."
    echo
    echo "# External NTP sources — master tracks real-world time"
    echo "pool ${pools[0]} iburst maxsources 4"
    local i
    for ((i = 1; i < ${#pools[@]}; i++)); do
      echo "pool ${pools[i]} iburst maxsources 2"
    done
    echo
    echo "# Big initial correction before serving clients (30 s timeout)"
    echo "initstepslew 30 ${pools[0]}"
    echo
    echo "# Fallback: serve local clock only when external sources are unreachable"
    echo "local stratum 10 orphan"
    echo
    echo "# Allow clients on the local network"
    local cidr
    for cidr in ${ALLOW_CIDRS[@]+"${ALLOW_CIDRS[@]}"}; do
      echo "allow $cidr"
    done
    echo
    echo "# Where chrony keeps its learned frequency estimate between restarts"
    echo "driftfile /var/lib/chrony/chrony.drift"
    echo
    echo "# Log directory"
    echo "logdir /var/log/chrony"
    echo
    echo "# Kernel sync (sync the RTC from the disciplined clock)"
    echo "rtcsync"
    echo
    echo "# Always step the clock if offset > 1 second"
    echo "makestep 1 -1"
  } > "$conf"
}

write_slave_conf() {
  local conf=/etc/chrony/chrony.conf
  log "Writing slave config -> $conf"

  if ((${#UPSTREAMS[@]})); then
    local pools=("${UPSTREAMS[@]}")
  else
    local pools=("time.google.com")
  fi

  {
    echo "# Chrony SLAVE config — generated by chrony-setup.sh ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
    echo "# NetRadar network: this node syncs to the fusion center."
    echo
    echo "# The fusion center is the PREFERRED source. prefer (not require) so"
    echo "# the internet pool below can take over if the fusion center is down."
    echo "# minpoll 0 / maxpoll 4 -> polls every 1 s up to every 16 s."
    echo "server $MASTER_IP trust prefer iburst minpoll 0 maxpoll 4"
    local s
    for s in ${SERVERS[@]+"${SERVERS[@]}"}; do
      echo "server $s iburst minpoll 0 maxpoll 4"
    done
    echo
    echo "# Fallback to internet NTP if no LAN server is reachable"
    local p
    for p in "${pools[@]}"; do
      echo "pool $p iburst"
    done
    echo
    echo "# Where chrony keeps its learned frequency estimate between restarts"
    echo "driftfile /var/lib/chrony/chrony.drift"
    echo
    echo "# Allow larger adjustments on the local network"
    echo "maxdistance 10.0"
    echo
    echo "# Gentle frequency slewing for sub-ms offsets"
    echo "smoothtime 400 0.001"
    echo
    echo "# Step only if offset > 1 s, only in the first 3 updates"
    echo "makestep 1.0 3"
    echo
    echo "# Log directory"
    echo "logdir /var/log/chrony"
    echo
    echo "# Kernel sync (sync the RTC from the disciplined clock)"
    echo "rtcsync"
  } > "$conf"
}

open_firewall() {
  # Only the master needs to accept inbound NTP (123/UDP).
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q 'Status: active'; then
    ufw allow 123/udp comment 'chrony NTP' >/dev/null
    log "ufw: allowed 123/udp"
  fi
  if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port=123/udp >/dev/null
    firewall-cmd --reload >/dev/null
    log "firewalld: allowed 123/udp"
  fi
}

restart_chrony() {
  # chrony.service can be masked (a symlink to /dev/null) on systems where
  # systemd-timesyncd previously took over — unmask it so we can enable/start.
  if [[ $(systemctl is-enabled chrony.service 2>/dev/null || echo unknown) == "masked" ]]; then
    log "chrony.service is masked — unmasking"
    systemctl unmask chrony.service
  fi
  # systemd-timesyncd is chrony's competitor (same clock, 123/323 ports); it
  # must not run alongside chrony.
  if systemctl is-active systemd-timesyncd >/dev/null 2>&1; then
    log "Stopping and disabling competing systemd-timesyncd"
    systemctl disable --now systemd-timesyncd >/dev/null 2>&1 || true
  fi
  systemctl enable chrony.service >/dev/null
  systemctl restart chrony.service
  systemctl --no-pager --quiet is-active chrony.service || die "chrony failed to start (journalctl -u chrony)"
}

verify() {
  log "chrony service: $(systemctl is-active chrony)"
  if ! chronyc tracking >/dev/null 2>&1; then
    log "chronyc tracking not responding yet — chrony may still be converging; try again in a few seconds"
    return 1
  fi
  echo "--- chronyc tracking ---"
  chronyc tracking | grep -E '^(Reference ID|Stratum|System time|Last offset|RMS offset|Leap status)'
  if [[ $ROLE == "slave" ]]; then
    echo "--- chronyc sources -v ---"
    chronyc sources -v
  fi
}

run_master() {
  ensure_installed

  if ((${#ALLOW_CIDRS[@]} == 0)); then
    local lan
    if lan=$(detect_lan_cidr); then
      ALLOW_CIDRS+=("$lan")
      log "Auto-detected LAN CIDR: $lan"
    fi
    # NetRadar nodes usually reach the fusion center over Tailscale.
    if ip link show tailscale0 >/dev/null 2>&1; then
      ALLOW_CIDRS+=("100.64.0.0/10")
      log "Tailscale detected; also serving 100.64.0.0/10"
    fi
    ((${#ALLOW_CIDRS[@]} > 0)) || die "Could not detect a client subnet; pass --allow <CIDR>"
  fi

  backup_config
  write_master_conf
  open_firewall
  restart_chrony

  echo
  log "Master (fusion center) configured. NTP clients reach it on 123/UDP from: ${ALLOW_CIDRS[*]}"
  echo "  Verify: chronyc tracking | chronyc clients (after nodes connect)"
  echo "  Nodes join with: sudo ./chrony-setup.sh slave <THIS_IP>"
}

run_slave() {
  # Accept an IP or a Tailscale DNS name; just reject anything with spaces.
  [[ $MASTER_IP =~ ^[^[:space:]]+$ ]] || die "invalid fusion center address: '$MASTER_IP'"
  local s
  for s in ${SERVERS[@]+"${SERVERS[@]}"}; do
    [[ $s =~ ^[^[:space:]]+$ ]] || die "invalid --server address: '$s'"
  done

  ensure_installed
  backup_config
  write_slave_conf
  restart_chrony

  echo
  log "Slave configured: preferred source $MASTER_IP (fusion center)"
  if ((${#SERVERS[@]})); then
    log "  Secondary LAN servers: ${SERVERS[*]}"
  fi
  log "  Internet pool fallback if no LAN server is reachable"
  echo "  Wait ~30 s for initial sync, then verify:"
  echo "  chronyc sources -v   # '^*' before the fusion center = synced"
  echo "  chronyc tracking     # System time = current offset"
  echo "  watch -n 1 'chronyc tracking'"
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
parse_args "$@"

case "$ROLE" in
  master|slave)
    if [[ ${EUID} -ne 0 ]]; then
      die "run as root: sudo $0 $*"
    fi
    ;;
esac

case "$ROLE" in
  master)
    run_master
    ;;
  slave)
    run_slave
    ;;
  verify)
    verify
    ;;
  "")
    die "missing role: master | slave | verify (see --help)"
    ;;
  *)
    die "unknown role: $ROLE (see --help)"
    ;;
esac
