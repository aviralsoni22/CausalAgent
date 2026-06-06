#!/bin/sh
# Sandbox entrypoint: lock down network egress, then drop privileges before
# starting the API. Runs as root only long enough to install the firewall rule.
#
# The container runs untrusted, LLM-generated R next to the extracted CSV — which
# is exactly the sensitive data Rule 2 keeps out of the LLM. Credentials are
# already isolated; this closes the other half of the threat model by denying the
# R process any way to send that data anywhere. We default the OUTPUT chain to
# DROP and only allow loopback plus replies on already-established (inbound)
# connections, so the API can still answer the orchestrator while a script's
# attempt to curl/download.file/open a socket simply goes nowhere.
set -e

# Apply the OUTPUT-DROP policy for one IP family: default deny, allow loopback and
# replies on already-established (inbound) connections. Returns non-zero if any
# rule fails so the caller can fail closed.
lock_down() {
    cmd="$1"
    "$cmd" -P OUTPUT DROP 2>/dev/null \
        && "$cmd" -A OUTPUT -o lo -j ACCEPT \
        && "$cmd" -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
}

# True only when IPv6 is actually usable in this network namespace. On an
# IPv4-only host ip6tables errors with "address family not supported"; that is
# not a lockdown failure because there is no v6 path to seal — so we skip it
# rather than fail closed. When v6 IS up, an open ip6tables OUTPUT chain is a
# wide-open exfiltration path, so the rule there is mandatory.
ipv6_active() {
    [ -f /proc/net/if_inet6 ] || return 1
    if [ -r /proc/sys/net/ipv6/conf/all/disable_ipv6 ]; then
        [ "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6)" = "0" ] || return 1
    fi
    return 0
}

fail_closed() {
    echo "[sandbox] FATAL: could not apply $1 egress lockdown — the container is" \
         "missing CAP_NET_ADMIN. Refusing to run untrusted R with open network" \
         "access. Grant the capability, or set SANDBOX_NETWORK_ISOLATION=0 only" \
         "if egress is blocked at the network layer instead." >&2
    exit 1
}

if [ "${SANDBOX_NETWORK_ISOLATION:-1}" != "0" ]; then
    lock_down iptables || fail_closed "IPv4"
    if ipv6_active; then
        lock_down ip6tables || fail_closed "IPv6"
        echo "[sandbox] egress lockdown applied (IPv4 + IPv6 OUTPUT policy DROP)."
    else
        echo "[sandbox] egress lockdown applied (IPv4 OUTPUT policy DROP; IPv6 inactive)."
    fi
fi

# Drop to an unprivileged uid for the actual service. setpriv ships with
# util-linux (already installed) so we avoid an extra gosu/su-exec dependency.
exec setpriv --reuid=10001 --regid=10001 --clear-groups "$@"
