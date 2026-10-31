#!/usr/bin/env bash
# =============================================================================
# setup.sh — prepare this machine as a NetRadar fusion center
#
#   ./setup.sh --nodes 3                  # nodes node1, node2, node3
#   ./setup.sh --nodes front,left,right   # your own node ids
#   ./setup.sh --add-node node4           # add a node later (others unchanged)
#   ./setup.sh --print-node node2         # show a node's block again
#
# Options:
#   --bind-ip <IP>   address the broker and dashboard listen on
#                    (default: this machine's Tailscale IPv4)
#   --host <name>    name/IP the nodes use to reach this machine
#                    (default: this machine's Tailscale MagicDNS name)
#   --force          regenerate ALL passwords (every node must be reconfigured)
#
# Writes (all git-ignored, readable only by you):
#   .env                 passwords for PostgreSQL and the server's MQTT user
#   nanomq/auth/<user>   one file per MQTT user, containing its password
#   node-configs/<id>.env  the block to give to each node's ./configure.sh
#
# Next: docker compose up -d --build   (see README.md)
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$REPO_DIR/.env"
AUTH_DIR="$REPO_DIR/nanomq/auth"
NODE_DIR="$REPO_DIR/node-configs"
SERVER_USER="server"

die()  { printf '\033[1;31m[setup] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }
warn() { printf '\033[1;33m[setup] WARNING:\033[0m %s\n' "$*" >&2; }
usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; }

rand_secret() {  # 48 hex chars; hex only, so it is safe in .env files and shells
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 24
  else python3 -c 'import secrets; print(secrets.token_hex(24))'; fi
}

env_get() {  # read KEY from .env (empty if absent)
  [[ -f $ENV_FILE ]] || return 0
  grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2- || true
}

NODES=() ADD_NODE="" PRINT_NODE="" BIND_IP="" FC_HOST="" FORCE=0
while (($#)); do
  case "$1" in
    --nodes)
      [[ -n ${2:-} ]] || die "--nodes needs a count or a comma-separated list"
      if [[ $2 =~ ^[0-9]+$ ]]; then
        for ((i = 1; i <= $2; i++)); do NODES+=("node$i"); done
      else
        IFS=',' read -r -a NODES <<<"$2"
      fi
      shift 2 ;;
    --add-node)   ADD_NODE="${2:?--add-node needs a node id}"; shift 2 ;;
    --print-node) PRINT_NODE="${2:?--print-node needs a node id}"; shift 2 ;;
    --bind-ip)    BIND_IP="${2:?--bind-ip needs an address}"; shift 2 ;;
    --host)       FC_HOST="${2:?--host needs a name or address}"; shift 2 ;;
    --force)      FORCE=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *)            die "unknown option: $1 (see --help)" ;;
  esac
done

print_block() {
  local id="$1" f="$NODE_DIR/$1.env"
  [[ -f $f ]] || die "no configuration for node '$id' (known: $(ls "$NODE_DIR" 2>/dev/null | sed 's/\.env$//' | tr '\n' ' '))"
  echo
  echo "# ---- node '$id': run ./configure.sh on that node and paste these lines, then Ctrl-D ----"
  cat "$f"
  echo "# ------------------------------------------------------------------------------"
}

if [[ -n $PRINT_NODE ]]; then print_block "$PRINT_NODE"; exit 0; fi

[[ $EUID -ne 0 ]] || warn "running as root; files will be owned by root (run as your normal user instead)"

# ---- existing nodes are kept; --add-node appends ------------------------------
if [[ -n $ADD_NODE ]]; then
  existing="$(env_get NETRADAR_NODES)"
  [[ -n $existing ]] || die "no existing setup found; run ./setup.sh --nodes <n> first"
  IFS=',' read -r -a NODES <<<"$existing"
  for n in "${NODES[@]}"; do [[ $n != "$ADD_NODE" ]] || die "node '$ADD_NODE' already exists"; done
  NODES+=("$ADD_NODE")
elif ((${#NODES[@]} == 0)); then
  existing="$(env_get NETRADAR_NODES)"
  [[ -n $existing ]] || die "say how many nodes: ./setup.sh --nodes 3   (see --help)"
  IFS=',' read -r -a NODES <<<"$existing"
fi
for n in "${NODES[@]}"; do
  [[ $n =~ ^[A-Za-z0-9_-]+$ ]] || die "invalid node id '$n' (letters, digits, '-' and '_' only)"
  [[ $n != "$SERVER_USER" ]] || die "'$SERVER_USER' is reserved for the fusion center's own MQTT user"
done

# ---- addresses -------------------------------------------------------------------
if [[ -z $BIND_IP ]]; then BIND_IP="$(env_get NETRADAR_BIND_IP)"; fi
if [[ -z $BIND_IP ]] && command -v tailscale >/dev/null 2>&1; then
  BIND_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
fi
[[ -n $BIND_IP ]] || die "no Tailscale IP found. Install Tailscale and run 'sudo tailscale up', or pass --bind-ip <IP>"

if [[ -z $FC_HOST ]]; then FC_HOST="$(env_get NETRADAR_FC_HOST)"; fi
if [[ -z $FC_HOST ]] && command -v tailscale >/dev/null 2>&1; then
  FC_HOST="$(tailscale status --json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
fi
[[ -n $FC_HOST ]] || FC_HOST="$BIND_IP"

# ---- secrets (kept across runs unless --force) -------------------------------------
PG_PASS="$(env_get POSTGRES_PASSWORD)"
SRV_PASS="$(env_get MQTT_SERVER_PASS)"
if ((FORCE == 1)) || [[ -z $PG_PASS ]]; then PG_PASS="$(rand_secret)"; fi
if ((FORCE == 1)) || [[ -z $SRV_PASS ]]; then SRV_PASS="$(rand_secret)"; fi

umask 077
mkdir -p "$AUTH_DIR" "$NODE_DIR"
chmod 700 "$AUTH_DIR" "$NODE_DIR"

tmp="$(mktemp "$REPO_DIR/.env.XXXXXX")"
cat > "$tmp" <<EOF
# Written by setup.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Secrets — never commit.
NETRADAR_BIND_IP=$BIND_IP
NETRADAR_FC_HOST=$FC_HOST
NETRADAR_NODES=$(IFS=,; echo "${NODES[*]}")
NETRADAR_TOPIC_PREFIX=netradar/v1
POSTGRES_USER=netradar
POSTGRES_DB=netradar
POSTGRES_PASSWORD=$PG_PASS
MQTT_SERVER_USER=$SERVER_USER
MQTT_SERVER_PASS=$SRV_PASS
EOF
mv "$tmp" "$ENV_FILE"

printf '%s' "$SRV_PASS" > "$AUTH_DIR/$SERVER_USER"
for n in "${NODES[@]}"; do
  if ((FORCE == 1)) || [[ ! -s $AUTH_DIR/$n ]]; then rand_secret | tr -d '\n' > "$AUTH_DIR/$n"; fi
  cat > "$NODE_DIR/$n.env" <<EOF
MQTT_HOST=$FC_HOST
MQTT_PORT=1883
NODE_ID=$n
MQTT_USERNAME=$n
MQTT_PASSWORD=$(cat "$AUTH_DIR/$n")
EOF
done
# remove broker users for nodes that are no longer listed
for f in "$AUTH_DIR"/*; do
  u="$(basename "$f")"
  [[ $u == "$SERVER_USER" || " ${NODES[*]} " == *" $u "* ]] || { rm -f "$f" "$NODE_DIR/$u.env"; echo "removed node '$u'"; }
done

echo "Fusion center configured: broker + dashboard on $BIND_IP, nodes reach it as '$FC_HOST'."
echo "Nodes: ${NODES[*]}"
if [[ -n $ADD_NODE ]]; then
  print_block "$ADD_NODE"
else
  for n in "${NODES[@]}"; do print_block "$n"; done
fi
cat <<EOF

Next:
  docker compose up -d --build              # start (or restart) the stack
  sudo ./time-sync/chrony-setup.sh master   # make this machine the time authority
  Dashboard: http://$FC_HOST:5002           (from any device on your tailnet)
EOF
if [[ -n $ADD_NODE ]] || ((FORCE == 1)); then
  echo "  Broker users changed: run 'docker compose restart nanomq' if the stack is already up."
fi
