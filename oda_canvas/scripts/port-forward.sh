#!/usr/bin/env bash
# Run `kubectl port-forward`s as systemd *user* services so they survive a closed terminal and are restarted if kubectl exits.
#   port-forward.sh start  <group> <context> <address> <ns>/<svc>=<local:remote[,local:remote]> ...
#   port-forward.sh stop   <group>
# One service per forward, named oda-pf-<group>-<n>.service. Starting a group replaces its previous services.
# After a logout/reboot they only come back with lingering: sudo loginctl enable-linger $USER
set -euo pipefail
cmd=${1:?}; group=${2:?}; shift 2
dir=$HOME/.config/systemd/user

stop_group() {
  local u
  for u in $(systemctl --user list-unit-files --no-legend "oda-pf-$group-*.service" | awk '{print $1}'); do
    systemctl --user disable --now "$u" 2>/dev/null || true
    rm -f "$dir/$u"
  done
  systemctl --user daemon-reload
}

case $cmd in
  stop) stop_group ;;
  start)
    ctx=${1:?}; addr=${2:?}; shift 2
    kubectl_bin=$(command -v kubectl)
    mkdir -p "$dir"; stop_group
    n=0; units=()
    for spec in "$@"; do
      target=${spec%%=*}; ports=${spec#*=}; ns=${target%%/*}; svc=${target#*/}
      n=$((n+1)); u=oda-pf-$group-$n.service; units+=("$u")
      cat > "$dir/$u" <<UNIT
[Unit]
Description=port-forward $ns/svc/$svc ($ports)

[Service]
ExecStart=$kubectl_bin --context $ctx -n $ns port-forward --address $addr svc/$svc ${ports//,/ }
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
UNIT
    done
    systemctl --user daemon-reload
    systemctl --user enable --now "${units[@]}" 2>&1 | grep -v '^Created symlink' || true
    sleep 2
    for u in "${units[@]}"; do printf '%s: %s\n' "$u" "$(systemctl --user is-active "$u" || true)"; done
    loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q yes || echo "note: lingering is off, services stop at logout: sudo loginctl enable-linger $USER"
    ;;
  *) echo "usage: $0 start|stop <group> ..." >&2; exit 2 ;;
esac
