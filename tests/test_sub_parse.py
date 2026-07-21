#!/usr/bin/env python3
"""Проверка парсера ссылок. Запуск: python3 test_parse.py"""
import base64, json
from app.skills.subscription.analyze import parse_link, to_links, flag_cc


def b64(s):
    return base64.b64encode(s.encode()).decode()


VMESS = "vmess://" + b64(json.dumps({
    "add": "v.example.com", "port": "8443", "id": "uuid-1", "net": "ws",
    "tls": "tls", "host": "cdn.example.com", "path": "/ray", "fp": "chrome"}))
CASES = {
    "vless": ("vless://uuid-2@a.example.com:443?encryption=none&flow=xtls-rprx-vision"
              "&type=tcp&security=reality&sni=www.microsoft.com&fp=chrome&pbk=KEY#Node%20🇩🇪",
              {"proto": "vless", "host": "a.example.com", "port": 443, "sec": "reality",
               "sni": "www.microsoft.com", "pbk": "KEY", "flow": "xtls-rprx-vision"}),
    "vmess": (VMESS, {"proto": "vmess", "host": "v.example.com", "port": 8443,
                      "net": "ws", "path": "/ray", "sni": "cdn.example.com"}),
    "trojan": ("trojan://pass123@t.example.com:2053?security=tls&type=ws&path=/tj#T",
               {"proto": "trojan", "host": "t.example.com", "port": 2053,
                "id": "pass123", "net": "ws", "path": "/tj"}),
    "ss-userinfo": ("ss://YWVzLTI1Ni1nY206cHc@s.example.com:8388#SS",
                    {"proto": "ss", "host": "s.example.com", "port": 8388}),
    "ss-blob": ("ss://" + b64("aes-256-gcm:pw@s2.example.com:9000") + "#SS2",
                {"proto": "ss", "host": "s2.example.com", "port": 9000}),
    "hysteria2": ("hy2://pw@h.example.com:443?sni=h.example.com&obfs=salamander#H",
                  {"proto": "hysteria2", "host": "h.example.com", "port": 443,
                   "sec": "встроенный", "extra": {"obfs": "salamander"}}),
    "tuic": ("tuic://uuid-3:pw@tu.example.com:443?alpn=h3&congestion_control=bbr#TU",
             {"proto": "tuic", "host": "tu.example.com", "port": 443,
              "sec": "встроенный"}),
}

for name, (link, want) in CASES.items():
    got = parse_link(link)
    assert got is not None, f"{name}: не распарсилось"
    for k, v in want.items():
        assert got[k] == v, f"{name}: {k} = {got[k]!r}, ожидалось {v!r}"
    print(f"ok  {name}")

# flag_cc
assert flag_cc("Node 🇩🇪") == "DE"
assert flag_cc("ЛАПА 🇫🇮") == "FI"
assert flag_cc("без флага") is None
print("ok  flag_cc")

# extra не тащит уже разложенные поля
assert "sni" not in parse_link(CASES["hysteria2"][0])["extra"]
print("ok  extra")

# три формата подписки
links = [l for l, _ in CASES.values()]
plain = "\n".join(links)
assert len(to_links(plain)[0]) == len(links), "plain-список"
assert len(to_links(b64(plain))[0]) == len(links), "base64-блоб"
cfg = json.dumps([{"outbounds": [{"protocol": "vless", "tag": "proxy", "settings": {
    "vnext": [{"address": "x.example.com", "port": 443, "users": [{"id": "u"}]}]}}]}])
assert to_links(cfg) == ([], json.loads(cfg)), "JSON-массив конфигов"
print("ok  форматы подписки")

# мусор не роняет парсер
assert parse_link("vless://") is None or parse_link("vless://")["host"] == ""
assert parse_link("не ссылка") is None
print("ok  мусор")


# render: секция «Вывод», сверка флага с ASN, кликабельный ASN
from app.skills.subscription.analyze import render

srv = [{"proto": "vless", "host": "a.example.com", "port": 443, "sec": "reality",
        "net": "tcp", "tag": "🇫🇮 FI-1", "sni": "x", "fp": "", "pbk": "", "flow": "",
        "path": "", "extra": {}}]
md = render("t", None, srv, [], {"a.example.com": ["1.2.3.4"]},
            {"1.2.3.4": {"as": "AS123 X", "countryCode": "RU", "country": "Russia", "org": "X"}})
assert "## Вывод" in md
assert "bgp.tools/as/123" in md, "ASN кликабельный"
assert "Метка страны врёт" in md, "FI-метка на RU-ASN"
assert "физически в России: 1" in md
print("ok  render")

print("\nвсе проверки пройдены")
