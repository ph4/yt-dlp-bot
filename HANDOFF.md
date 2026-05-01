# Telegram DPI Bypass — Handoff

**Date**: 2026-05-02  
**Repo**: `github.com/ph4/yt-dlp-bot`  
**Machine**: `ru.yiff.fi` (EPYC 9634, EL9, 31.130.144.133)

## Current state

| Path | Status | Notes |
|---|---|---|
| Local → bot API (`api.telegram.org`) | ✅ 200 OK | nfqws2 fake+multisplit working on TLS ClientHello |
| Local → DC IPs (TCP handshake) | ✅ connects | `curl https://149.154.167.220` succeeds |
| yt-dlp-bot polling | ✅ stable | No timeouts since `--ipcache-lifetime=0` fix |
| WG clients → TG DCs (MTProto) | ❌ blocked | Packets hit queue, desync applies, but clients fail |

## What's deployed

### zapret2 (`bol-van/zapret2`)
- Source: `/opt/zapret2/` (git clone)
- Binary: `/opt/zapret2/binaries/my/nfqws2`
- Lua: `/opt/zapret2/lua/zapret-lib.lua`, `zapret-antidpi.lua`

### nftables (`table inet zapret`)
- `post` chain: `type filter hook postrouting` — queues TCP 80/443/5222 to Telegram IPs → NFQUEUE 200
- `pre` chain: `type filter hook prerouting` — queues reply packets
- `predefrag` chain: `type filter hook output priority -401` — `notrack` on marked packets
- Sysctl: `net.netfilter.nf_conntrack_tcp_be_liberal=1`
- Config: `/opt/zapret2/nftables.conf` (generated from `~/portable/nftables.conf.template`)

### Telegram DC IP ranges tracked
```
149.154.160.0/20      # Main DCs (includes api.telegram.org)
91.108.4.0/22          # DC1 range
91.108.8.0/22          # DC2 range
91.108.12.0/22         # DC3 range
91.108.16.0/22         # DC4 range
91.108.20.0/22         # DC5 range
91.108.56.0/22         # Backup range
185.76.145.0/24        # Additional DC
```
IPv6: `2001:67c:4e8::/48`, `2001:b28:f23d::/48`, `2001:b28:f23f::/48`

### systemd units
| Unit | Type | Description |
|---|---|---|
| `zapret-nftables.service` | oneshot | Loads nftables rules before networking |
| `nfqws2.service` | simple | Desync daemon on queue 200 |
| `yt-dlp-bot.service` (user) | simple | Bot polling |

Boot order: `zapret-nftables → nfqws2 → yt-dlp-bot`

### Default desync strategy
```
fake:tcp_md5 (fake TLS ClientHello with bad MD5)
  → multisplit:pos=1,midsld (split real ClientHello into 3 TCP segments)
```
Configurable via `~/portable/strategy.conf`.

## Known issues & theories for WG client failure

nfqws2 processes WG client packets (confirmed in journal: TLS ClientHello detected, multisplit applied, 3 segments sent). Connection still fails.

### Theory A — tcp_md5 is too aggressive
`fake:tcp_md5` sends packet with invalid TCP MD5 signature. Intermediate routers may drop it before reaching TG DCs. The legitimate segments that follow may arrive out of sequence.

**Fix**: Try `datanoack` or `badseq` instead of `tcp_md5`:
```ini
NFQWS2_OPTS="--payload=tls_client_hello --lua-desync=fake:blob=fake_default_tls:datanoack --lua-desync=multisplit:pos=1,midsld"
```

### Theory B — MTProto not matched by L7 filter
`--filter-l7=tls,http` won't match MTProto obfuscated init packets. MTProto init goes through profile 0 (no desync).

**Fix**: Remove L7 filter or add MTProto-specific profile.

### Theory C — DNS poisoning
RKN may be poisoning DNS for TG domains. WG clients resolve `pluto.telegram.org` to wrong IPs.

**Fix**: Set WG client DNS to `1.1.1.1` or `8.8.8.8`, or use DoH.

### Theory D — ISP-specific strategy needed
Default strategy may not work for your ISP.

**Fix**: Run blockcheck to find working strategy:
```bash
cd /opt/zapret2 && ./blockcheck2.sh
```

### Theory E — Conntrack window too narrow
`ct original packets 1-12` may not cover enough MTProto handshake packets.

**Fix**: Increase to `packets 1-30` in nftables rules.

## Debugging

```bash
# Watch nfqws2 in real time
systemctl stop nfqws2
/opt/zapret2/binaries/my/nfqws2 --qnum 200 --debug --lua-init=@/opt/zapret2/lua/zapret-lib.lua --lua-init=@/opt/zapret2/lua/zapret-antidpi.lua --filter-tcp=80,443 $(grep NFQWS2_OPTS ~/portable/strategy.conf | cut -d= -f2- | tr -d '"')

# Check if nftables is queueing
nft list chain inet zapret post | grep counter

# Check conntrack for TG connections
conntrack -L -p tcp --dport 443 | grep -E "149\.154"

# Test raw TCP
curl -sk https://149.154.167.220 --connect-timeout 5
curl -sk https://api.telegram.org/bot<TOKEN>/getMe

# Bot logs
journalctl --user -u yt-dlp-bot -f
journalctl -u nfqws2 -f
```

## Turn off zapret

```bash
systemctl disable --now nfqws2 zapret-nftables
nft delete table inet zapret
sysctl net.netfilter.nf_conntrack_tcp_be_liberal=0
systemctl --user stop yt-dlp-bot
```

## Fallback: tg-ws-proxy (MTProto via CloudFlare WS)

If zapret can't reliably crack DPI for MTProto, run a WSS-based proxy:

```bash
# Python version (Flowseal upstream)
pip install git+https://github.com/Flowseal/tg-ws-proxy.git
tg-ws-proxy --port 1443 --dc-ip 2:149.154.167.220 --dc-ip 4:149.154.167.220

# TG clients connect to SOCKS5 127.0.0.1:1443
```

This proxies MTProto through CloudFlare WebSocket — different path vs zapret's packet mangling. Can run alongside zapret (zapret for bot, tg-ws-proxy for MTProto clients).

## File inventory

```
~/portable/
  setup-zapret.sh          # Full install script
  teardown-zapret.sh        # Clean removal
  strategy.conf             # Editable desync params
  nftables.conf.template    # nftables rules template
  DECISIONS.md              # Architecture rationale

/opt/zapret2/
  nftables.conf             # Active nftables rules
  binaries/my/nfqws2        # Compiled binary
  lua/                      # Strategy scripts

/etc/systemd/system/
  zapret-nftables.service
  nfqws2.service

/etc/sysctl.d/
  99-zapret.conf
