"""Разбор VPN-подписки (URL подписки или сырые ссылки) в компактный markdown-отчёт.

Вся тяжёлая работа здесь: фетч, парсинг, резолв, ASN. В контекст агента
попадает только итоговый markdown (см. report()).

Перенесено из Claude Code skill `sub-report` — синхронный stdlib-код,
вызывать через asyncio.to_thread.
"""
import base64, json, os, re, socket, sys, urllib.parse, urllib.request, uuid
from collections import Counter, defaultdict

UAS = ["Hiddify/2.5.7", "v2rayNG/1.8.5", "Happ/1.0", "Streisand", "FoXray/1.5",
       "clash-verge/1.5.0", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"]
# Панели с HWID-гейтингом (Remnawave) без этих заголовков отдают заглушку.
STUB = ("0.0.0.0", "не поддерживается", "not supported")
# Отдельно: перебор UA тут не поможет и только жжёт лимит устройств.
HWID_LIMIT = ("Лимит устройств", "device limit", "HWID")
# HWID обязан быть стабильным между запусками: новый = +1 устройство к лимиту.
HWID_FILE = os.getenv("SUB_HWID_FILE", "/data/hwid.txt")
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

def hwid():
    """Стабильный ID устройства. Не переживёт перезапуск без тома — тогда
    подписка увидит новое устройство."""
    try:
        if os.path.exists(HWID_FILE):
            return open(HWID_FILE).read().strip()
        v = uuid.uuid4().hex[:16]
        os.makedirs(os.path.dirname(HWID_FILE), exist_ok=True)
        open(HWID_FILE, "w").write(v)
        return v
    except OSError:
        return uuid.uuid4().hex[:16]


def fetch(url):
    """Перебирает User-Agent'ы, пока панель не отдаст непустое тело."""
    last, hw = "", hwid()
    for ua in UAS:
        h = {"User-Agent": ua, "Accept": "*/*",
             "x-hwid": hw, "x-device-os": "android", "x-ver-os": "14",
             "x-device-model": "Pixel 7"}
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read().decode("utf-8", "replace").strip()
            # base64-тела: маркеры заглушек видны только после декодирования
            probe = body
            try:
                probe += urllib.parse.unquote(b64d(body).decode("utf-8", "replace"))
            except Exception:
                probe += urllib.parse.unquote(body)
            if body and any(m in probe for m in HWID_LIMIT):
                raise ValueError(
                    "подписка отдаёт заглушку «лимит устройств»: HWID исчерпан. "
                    f"Сбросьте устройства в панели или удалите {HWID_FILE}")
            if body and not any(m in probe for m in STUB):
                # Тело может быть валидным, но чужого формата (sing-box под
                # Hiddify) — тогда серверов из него не достать, идём к след. UA.
                if to_servers(body)[0]:
                    return body, ua
                last = f"формат не разобран на UA={ua}"
                continue
            last = ("заглушка" if body else "пустое тело") + f" на UA={ua}"
        except ValueError:
            raise
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


def to_servers(body):
    """Тело подписки -> (servers, xray_configs). Пустой список серверов =
    формат не наш."""
    links, cfgs = to_links(body)
    servers = []
    for c in cfgs:
        for ob in c.get("outbounds") or []:
            if ob.get("protocol") in ("vless", "vmess", "trojan", "shadowsocks"):
                servers += parse_outbound(ob)
    servers += [s for s in map(parse_link, links) if s and s["host"]]
    return servers, cfgs


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


def hoster_sites(as_strs):
    """{'AS15169 Google LLC', ...} -> {asn: website}. PeeringDB, без ключа."""
    asns = {m.group(1) for s in as_strs if s for m in [re.match(r"AS(\d+)", s)] if m}
    if not asns:
        return {}
    try:
        req = urllib.request.Request(
            "https://www.peeringdb.com/api/net?fields=asn,website&asn__in="
            + ",".join(sorted(asns)), headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return {str(d["asn"]): d.get("website") or "" for d in json.load(r)["data"]}
    except Exception as e:
        print(f"warn: peeringdb недоступен: {e}", file=sys.stderr)
        return {}


def as_link(as_str):
    """'AS15169 Google LLC' -> кликабельная ссылка на карточку ASN."""
    if not as_str:
        return "—"
    m = re.match(r"AS(\d+)", as_str)
    return f"[{as_str}](https://bgp.tools/as/{m.group(1)})" if m else as_str


def top(counter, n=25):
    return [(k or "—", v) for k, v in counter.most_common(n)]


def render(src, ua, servers, cfgs, ipmap, geomap):
    L = [f"# Отчёт по подписке: {src}", ""]
    L.append(f"_Формат ответа: {'JSON-массив Xray-конфигов' if cfgs else 'список ссылок'}"
             + (f" · рабочий User-Agent: `{ua}`" if ua else "") + "_\n")

    # Хост, резолвящийся в кучу IP, — не выделенный сервер, а SNI-фронт/decoy
    # (google.com и т.п.). Считать его машиной и хостингом = врать в статистике.
    # ponytail: порог 2, реальные ноды дают 1 IP (иногда +IPv6). Поднять, если ловит своих.
    decoy = {h for h, ips in ipmap.items() if len(ips) > 2}
    real_ips = {ip for h, ips in ipmap.items() if h not in decoy for ip in ips}

    def asn_of(h):
        ips = ipmap.get(h) or []
        g = geomap.get(ips[0], {}) if ips else {}
        return (f"{len(ips)} IP (фронт)" if h in decoy else (ips[0] if ips else "—"),
                as_link(g.get("as")), g.get("countryCode"),
                g.get("country", "—"), g.get("org") or g.get("isp") or "—")

    # Один IP под несколькими именами: узлов в подписке больше, чем машин.
    byip = defaultdict(set)
    for h, ips in ipmap.items():
        if h not in decoy:
            for ip in ips:
                byip[ip].add(h)
    dup = {ip: hs for ip, hs in byip.items() if len(hs) > 1}
    nmach = len(byip) or len({s["host"] for s in servers})

    # Одна машина не может стоять в двух странах: если её входной IP обслуживает
    # узлы с разными заявленными странами — это вход каскада, а не сам выход.
    claims_by_ip = defaultdict(set)
    for s in servers:
        c, ips = flag_cc(s.get("tag") or ""), ipmap.get(s["host"]) or []
        if c and ips:
            claims_by_ip[ips[0]].add(c)

    # Единая таблица: название из подписки + фактическая топология.
    rows, mismatch = [], []
    for s in sorted(servers, key=lambda x: (not x.get("tag"), x.get("tag") or x["host"])):
        ip, asn, cc, country, org = asn_of(s["host"])
        claim = flag_cc(s.get("tag") or "")
        warn = ""
        if claim and cc and claim != cc and claim != "EU":
            ips = ipmap.get(s["host"]) or []
            # Каскад: общий вход на несколько стран или вход за CDN-фронтом.
            casc = s["host"] in decoy or len(claims_by_ip.get(ips[0] if ips else "", ())) > 1
            warn = " (каскад)" if casc else " (≠)"
            mismatch.append((s.get("tag") or s["host"], claim, cc, casc))
        rows.append([s.get("tag") or "—", s["proto"], s["host"], ip, country, asn + warn, org])

    hosters = Counter(geomap.get(ip, {}).get("as") or ""
                      for ip in real_ips)

    # --- Вывод: то, ради чего отчёт читают. Из уже посчитанных фактов.
    facts = []
    if len(servers) > nmach:
        facts.append(f"**{len(servers)} узлов в подписке — на {nmach} реальных машинах.**")
    if decoy:
        facts.append("Узлы на " + ", ".join(f"`{h}`" for h in sorted(decoy))
                     + " — маскировочный адрес, а не отдельный сервер.")
    if mismatch:
        nc = sum(1 for *_, c in mismatch if c)
        facts.append(
            f"**У {len(mismatch)} узлов метка страны не совпадает с точкой входа**"
            + (f" (из них {nc} с признаками каскада)" if nc else "") + ", см. ниже.")
    sec = Counter(s["sec"] for s in servers)
    facts.append("Шифрование: " + ", ".join(f"{k or 'нет'} ({v})" for k, v in sec.most_common()) + ".")
    cc_cnt = Counter(geomap.get(ip, {}).get("countryCode") or "?" for ip in real_ips)
    if cc_cnt:
        facts.append("Реальная география: "
                     + ", ".join(f"{k} ({v})" for k, v in cc_cnt.most_common()) + ".")
    ru = [ip for ip in real_ips if geomap.get(ip, {}).get("countryCode") == "RU"]
    if ru:
        facts.append(f"**Точек входа в России: {len(ru)}** — на них трафик остаётся "
                     "в юрисдикции, если за узлом нет каскада наружу.")
    L += ["## Вывод", ""] + [f"- {f}" for f in facts] + [""]

    L += ["## Сводка", ""]
    L.append(md_table(["Метрика", "Значение"], [
        ("Узлов в подписке", len(servers)),
        ("Реальных машин", nmach),
        ("Профилей/конфигов", len(cfgs) or "—"),
        ("Протоколы", ", ".join(f"{k} ({v})" for k, v in Counter(s["proto"] for s in servers).most_common())),
        ("Транспорты", ", ".join(f"{k} ({v})" for k, v in Counter(s["net"] for s in servers).most_common())),
        ("Безопасность", ", ".join(f"{k} ({v})" for k, v in sec.most_common())),
        ("Порты", ", ".join(f"{k} ({v})" for k, v in Counter(s["port"] for s in servers).most_common())),
    ]))

    L += ["", "## Узлы, IP и хостинг", ""]
    L.append(md_table(["Название в подписке", "Протокол", "Хост", "IP",
                       "Страна", "ASN", "Организация"], rows))
    if mismatch:
        L += ["", "**Метка страны не совпадает с точкой входа:**", "",
              "_Резолвится только вход. Пометка «каскад» — вход обслуживает несколько "
              "заявленных стран или стоит за CDN-фронтом, значит выход находится "
              "в другом месте и по этим данным не определяется._", ""]
        L += [f"- `{t}` — заявлено {c}, вход в {a}" + (" (каскад)" if k else "")
              for t, c, a, k in mismatch]
        L.append("")
    if dup:
        L += ["Один IP под несколькими именами:", ""]
        L += [f"- `{ip}` ← {', '.join(sorted(hs))}" for ip, hs in sorted(dup.items())]
        L.append("")

    if hosters:
        sites = hoster_sites(hosters)  # пустой hosters при --no-net -> без запроса

        def site_of(as_str):
            m = re.match(r"AS(\d+)", as_str or "")
            url = sites.get(m.group(1)) if m else None
            return f"<{url}>" if url else "—"

        L += ["", "### Распределение по хостингам", "",
              md_table(["Хостинг / ASN", "IP", "Сайт"],
                       [(as_link(k) if k else "неизвестно", v, site_of(k))
                        for k, v in hosters.most_common(25)]),
              "_Маскировочные хосты не учтены. Сайты — PeeringDB._" if decoy
              else "_Сайты — PeeringDB._"]

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

    servers, cfgs = to_servers(body)
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
