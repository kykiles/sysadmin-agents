"""Разбор VPN-подписки (URL подписки или сырые ссылки) в компактный markdown-отчёт.

Вся тяжёлая работа здесь: фетч, парсинг, резолв, ASN. В контекст агента
попадает только итоговый markdown (см. report()).

Перенесено из Claude Code skill `sub-report` — синхронный stdlib-код,
вызывать через asyncio.to_thread.
"""
import base64, json, re, socket, sys, urllib.parse, urllib.request
from collections import Counter, defaultdict

UAS = ["v2rayNG/1.8.5", "Happ/1.0", "Streisand", "clash-verge/1.5.0",
       "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"]
PROTOS = ("vless://", "vmess://", "trojan://", "ss://", "hysteria2://", "hy2://", "tuic://")
# Параметры, которые уже разложены по полям; остальное уходит в extra как есть.
KNOWN_Q = {"encryption", "flow", "type", "security", "sni", "host", "peer", "fp",
           "pbk", "path", "headerType", "serviceName", "mode", "seed"}
# У hysteria2/tuic шифрование внутри протокола, отдельного security= в ссылке нет.
BUILTIN_TLS = ("hysteria2", "tuic")


def flag_cc(s):
    """🇫🇮 -> 'FI'. None, если флага в строке нет."""
    cps = [ord(c) - 0x1F1E6 for c in s if 0x1F1E6 <= ord(c) <= 0x1F1FF]
    return (chr(cps[0] + 65) + chr(cps[1] + 65)) if len(cps) >= 2 else None


def b64d(s):
    s = s.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


# --- fetch ------------------------------------------------------------------

def fetch(url, hwid=None):
    """Перебирает User-Agent'ы, пока панель не отдаст непустое тело."""
    last = ""
    for ua in UAS:
        h = {"User-Agent": ua, "Accept": "*/*"}
        if hwid:
            h.update({"x-hwid": hwid, "x-device-os": "linux", "x-device-model": "PC"})
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read().decode("utf-8", "replace").strip()
            if body:
                return body, ua
            last = f"пустое тело на UA={ua}"
        except Exception as e:
            last = f"{ua}: {e}"
    raise ValueError(f"не удалось скачать подписку ({last})")


# --- parse ------------------------------------------------------------------

def to_links(body):
    """Возвращает (links, xray_configs). Форматы: JSON-массив конфигов,
    base64-блоб, plain-список ссылок."""
    if body.lstrip().startswith(("[", "{")):
        try:
            data = json.loads(body)
            cfgs = data if isinstance(data, list) else [data]
            if any(isinstance(c, dict) and "outbounds" in c for c in cfgs):
                return [], cfgs
        except json.JSONDecodeError:
            pass
    if not any(p in body[:4000] for p in PROTOS):
        try:
            body = b64d(body).decode("utf-8", "replace")
        except Exception:
            pass
    return [l.strip() for l in body.splitlines() if l.strip().startswith(PROTOS)], []


def parse_link(link):
    """Голая ссылка -> dict сервера. None если не разобрали."""
    try:
        scheme, rest = link.split("://", 1)
        scheme = {"hy2": "hysteria2"}.get(scheme, scheme)
        if scheme == "vmess":
            j = json.loads(b64d(rest.split("#")[0]))
            return {"proto": "vmess", "host": j.get("add", ""), "port": int(j.get("port", 0) or 0),
                    "id": j.get("id", ""), "sni": j.get("sni") or j.get("host", ""),
                    "net": j.get("net", "tcp"), "sec": j.get("tls", "") or "none",
                    "path": j.get("path", ""), "fp": j.get("fp", ""), "pbk": "", "flow": "",
                    "extra": {},
                    "tag": urllib.parse.unquote(rest.split("#", 1)[1]) if "#" in rest else ""}
        u = urllib.parse.urlsplit(link)
        q = dict(urllib.parse.parse_qsl(u.query))
        uid = urllib.parse.unquote(u.username or "")
        if scheme == "ss" and "@" not in rest.split("#")[0]:  # ss://base64(all)
            dec = b64d(rest.split("#")[0]).decode("utf-8", "replace")
            u = urllib.parse.urlsplit("ss://" + dec)
            uid = urllib.parse.unquote(u.username or "")
        sec = q.get("security") or ("встроенный" if scheme in BUILTIN_TLS else "none")
        return {"proto": scheme, "host": u.hostname or "", "port": u.port or 0, "id": uid,
                "sni": q.get("sni") or q.get("host") or q.get("peer", ""),
                "net": q.get("type", "quic" if scheme in BUILTIN_TLS else "tcp"), "sec": sec,
                "path": q.get("path", ""), "fp": q.get("fp", ""), "pbk": q.get("pbk", ""),
                "flow": q.get("flow", ""),
                "extra": {k: v for k, v in q.items() if k not in KNOWN_Q},
                "tag": urllib.parse.unquote(u.fragment)}
    except Exception:
        return None


def parse_outbound(ob):
    """Xray outbound -> dict сервера."""
    p, s = ob.get("protocol", ""), ob.get("settings") or {}
    ss = ob.get("streamSettings") or {}
    tls = ss.get("tlsSettings") or ss.get("realitySettings") or {}
    net = ss.get("network", "tcp")
    path = ((ss.get("wsSettings") or ss.get("xhttpSettings")
             or ss.get("httpSettings") or {}).get("path", ""))
    base = {"proto": p, "net": net, "sec": ss.get("security", "none"), "path": path,
            "sni": tls.get("serverName", ""), "fp": tls.get("fingerprint", ""),
            "pbk": tls.get("publicKey", ""), "tag": ob.get("tag", ""), "extra": {}}
    if p in ("vless", "vmess"):
        out = []
        for v in s.get("vnext") or []:
            users = v.get("users") or [{}]
            out.append({**base, "host": v.get("address", ""), "port": v.get("port", 0),
                        "id": users[0].get("id", ""), "flow": users[0].get("flow", "")})
        return out
    for srv in s.get("servers") or []:
        return [{**base, "host": srv.get("address", ""), "port": srv.get("port", 0),
                 "id": srv.get("password", ""), "flow": ""}]
    return []


# --- enrich -----------------------------------------------------------------

def resolve(hosts):
    ips = {}
    for h in hosts:
        if re.fullmatch(r"[\d.]+", h) or ":" in h:
            ips[h] = [h]
            continue
        try:
            ips[h] = sorted({ai[4][0] for ai in socket.getaddrinfo(h, None)})
        except OSError:
            ips[h] = []
    return ips


def geo(ips):
    """ip-api.com/batch: страна, провайдер, ASN, признак хостинга."""
    out = {}
    ips = list(ips)
    for i in range(0, len(ips), 100):
        chunk = ips[i:i + 100]
        payload = json.dumps([{"query": ip} for ip in chunk]).encode()
        try:
            req = urllib.request.Request(
                "http://ip-api.com/batch?fields=status,query,country,countryCode,city,isp,org,as,hosting",
                data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                for rec in json.load(r):
                    if rec.get("status") == "success":
                        out[rec["query"]] = rec
        except Exception as e:
            print(f"warn: ip-api недоступен: {e}", file=sys.stderr)
            break
    return out


# --- render -----------------------------------------------------------------

def md_table(head, rows):
    if not rows:
        return "_нет данных_\n"
    return ("| " + " | ".join(head) + " |\n| " + " | ".join("---" for _ in head) + " |\n"
            + "".join("| " + " | ".join(str(c) for c in r) + " |\n" for r in rows))


def top(counter, n=25):
    return [(k or "—", v) for k, v in counter.most_common(n)]


def render(src, ua, servers, cfgs, ipmap, geomap):
    L = [f"# Отчёт по подписке: {src}", ""]
    L.append(f"_Формат ответа: {'JSON-массив Xray-конфигов' if cfgs else 'список ссылок'}"
             + (f" · рабочий User-Agent: `{ua}`" if ua else "") + "_\n")

    L += ["## Сводка", ""]
    L.append(md_table(["Метрика", "Значение"], [
        ("Уникальных серверов", len(servers)),
        ("Уникальных хостов", len({s["host"] for s in servers})),
        ("Профилей/конфигов", len(cfgs) or "—"),
        ("Протоколы", ", ".join(f"{k} ({v})" for k, v in Counter(s["proto"] for s in servers).most_common())),
        ("Транспорты", ", ".join(f"{k} ({v})" for k, v in Counter(s["net"] for s in servers).most_common())),
        ("Безопасность", ", ".join(f"{k} ({v})" for k, v in Counter(s["sec"] for s in servers).most_common())),
        ("Порты", ", ".join(f"{k} ({v})" for k, v in Counter(s["port"] for s in servers).most_common())),
    ]))

    # Названия узлов из подписки рядом с фактическим IP/ASN — сопоставление
    # обещанного с реальным. Плюс проверка флага в названии против страны ASN.
    def asn_of(h):
        ips = ipmap.get(h) or []
        g = geomap.get(ips[0], {}) if ips else {}
        return (ips[0] if ips else "—",
                f"{g.get('as', '') or '?'}" + (f" ({g.get('countryCode')})" if g.get("countryCode") else ""),
                g.get("countryCode"))

    named = [s for s in servers if s.get("tag")]
    if named:
        L += ["", "## Названия узлов и реальная топология", ""]
        rows, mismatch = [], []
        for s in named:
            ip, asn, cc = asn_of(s["host"])
            claim = flag_cc(s["tag"])
            warn = ""
            if claim and cc and claim != cc:
                warn, _ = " ⚠️", mismatch.append((s["tag"], claim, cc))
            rows.append([s["tag"], s["proto"], s["host"], ip, asn + warn])
        L.append(md_table(["Название в подписке", "Протокол", "Хост", "IP", "ASN"], rows))
        if mismatch:
            L += ["", "**Метка страны не совпадает с ASN:**", ""]
            L += [f"- `{t}` — заявлено {c}, фактически {a}" for t, c, a in mismatch]
            L.append("")

    # Один IP под несколькими именами: узлов в подписке больше, чем машин.
    byip = defaultdict(set)
    for h, ips in ipmap.items():
        for ip in ips:
            byip[ip].add(h)
    dup = {ip: hs for ip, hs in byip.items() if len(hs) > 1}
    nmach = len(byip) or len({s["host"] for s in servers})
    if len(servers) > nmach:
        L += ["", f"**Уникальных IP: {nmach} при {len(servers)} узлах в подписке.**", ""]
    if dup:
        L += ["Один IP под несколькими именами:", ""]
        L += [f"- `{ip}` ← {', '.join(sorted(hs))}" for ip, hs in sorted(dup.items())]
        L.append("")

    L += ["", "## Серверы, IP и хостинг", ""]
    rows = []
    for h in sorted({s["host"] for s in servers}):
        for ip in (ipmap.get(h) or ["—"]):
            g = geomap.get(ip, {})
            rows.append([h, ip, g.get("country", "—"),
                         (g.get("as") or "—")[:40], g.get("org") or g.get("isp") or "—",
                         "да" if g.get("hosting") else ""])
    L.append(md_table(["Хост", "IP", "Страна", "ASN", "Организация", "Хостинг"], rows))

    hosters = Counter((geomap.get(ip, {}).get("as") or "неизвестно").split(" ", 1)[-1]
                      for ips in ipmap.values() for ip in ips)
    if hosters:
        L += ["", "### Распределение по хостингам", "",
              md_table(["Хостинг / ASN", "IP"], top(hosters))]

    for title, key in [("SNI / маскировочные домены", "sni"), ("Fingerprint (uTLS)", "fp"),
                       ("Reality public key", "pbk"), ("Flow", "flow"), ("Path", "path")]:
        c = Counter(s[key] for s in servers if s.get(key))
        if c:
            L += ["", f"### {title}", "", md_table([title.split(" ")[0], "Серверов"], top(c))]

    # Нераспознанные query-параметры: клиентские обходы DPI (fm/fragment), alpn,
    # obfs и прочее, что зависит от панели. Показываем как есть.
    ex = defaultdict(set)
    for s in servers:
        for k, v in (s.get("extra") or {}).items():
            ex[k].add(v)
    if ex:
        L += ["", "## Прочие параметры ссылок", ""]
        for k, vals in sorted(ex.items()):
            L.append(f"- **{k}** ({len(vals)} вариант(ов)):")
            for v in sorted(vals)[:5]:
                L.append(f"  - `{urllib.parse.unquote(v)[:400]}`")
        L.append("")

    if cfgs:
        L += ["", "## Маршрутизация", ""]
        for i, c in enumerate(cfgs):
            r = c.get("routing") or {}
            rules, bals = r.get("rules") or [], r.get("balancers") or []
            name = (c.get("remarks") or c.get("tag") or f"конфиг #{i + 1}")
            L.append(f"### {name}")
            L.append(f"- правил: **{len(rules)}**, стратегия домена: `{r.get('domainStrategy', '—')}`")
            if bals:
                L.append(f"- балансировщики: **{len(bals)}**")
                L.append(md_table(["Тег", "Селекторы", "Стратегия"],
                                  [(b.get("tag", "—"), ", ".join(b.get("selector") or []) or "—",
                                    (b.get("strategy") or {}).get("type", "—")) for b in bals]))
            else:
                L.append("- балансировщиков нет")
            if c.get("burstObservatory") or c.get("observatory"):
                L.append("- есть observatory (активные пробы латентности)")
            act = Counter((rl.get("balancerTag") or rl.get("outboundTag") or "—") for rl in rules)
            L.append("")
            L.append(md_table(["Действие правила", "Правил"], top(act, 15)))
            dom = sum(len(rl.get("domain") or []) for rl in rules)
            ipr = sum(len(rl.get("ip") or []) for rl in rules)
            L.append(f"\nДоменных критериев: {dom}, IP-критериев: {ipr}\n")
            for rl in rules[:40]:
                crit = []
                for k in ("domain", "ip", "port", "protocol", "network", "inboundTag"):
                    if rl.get(k):
                        v = rl[k]
                        v = ", ".join(map(str, v))[:110] if isinstance(v, list) else str(v)
                        crit.append(f"{k}={v}")
                L.append(f"- `{rl.get('balancerTag') or rl.get('outboundTag') or '?'}` ← "
                         + ("; ".join(crit) or "всё остальное"))
            if len(rules) > 40:
                L.append(f"- _…ещё {len(rules) - 40} правил_")
            L.append("")
            dns = c.get("dns") or {}
            if dns:
                srv = []
                for d in dns.get("servers") or []:
                    srv.append(d if isinstance(d, str) else
                               f"{d.get('address', '?')} (домены: {len(d.get('domains') or [])})")
                L += ["#### DNS", "", f"- серверы: {', '.join(srv) or '—'}",
                      f"- queryStrategy: `{dns.get('queryStrategy', '—')}`",
                      f"- статических хостов: {len(dns.get('hosts') or {})}", ""]
            if len(cfgs) > 1 and i == 0:
                same = all(json.dumps(x.get("routing"), sort_keys=True)
                           == json.dumps(c.get("routing"), sort_keys=True) for x in cfgs)
                if same:
                    L.append("_Маршрутизация идентична во всех конфигах — остальные опущены._\n")
                    break
    return "\n".join(L) + "\n"


# --- entrypoint -------------------------------------------------------------

def report(source, no_net=False):
    """source: URL подписки или текст со ссылками vless://…. Возвращает markdown."""
    source = source.strip()
    ua = None
    if source.startswith(("http://", "https://")) and not source.startswith(PROTOS):
        body, ua = fetch(source)
        src = urllib.parse.urlsplit(source).hostname or source
    else:
        body, src = source, "links"

    links, cfgs = to_links(body)
    servers = []
    for c in cfgs:
        for ob in c.get("outbounds") or []:
            if ob.get("protocol") in ("vless", "vmess", "trojan", "shadowsocks"):
                servers += parse_outbound(ob)
    servers += [s for s in map(parse_link, links) if s and s["host"]]
    if not servers:
        raise ValueError("серверы не найдены — формат подписки не распознан")

    seen, uniq = set(), []
    for s in servers:
        k = (s["proto"], s["host"], s["port"], s.get("id", ""))
        if k not in seen:
            seen.add(k)
            uniq.append(s)

    ipmap = {} if no_net else resolve({s["host"] for s in uniq})
    geomap = {} if no_net else geo({ip for v in ipmap.values() for ip in v})
    return render(src, ua, uniq, cfgs, ipmap, geomap)
