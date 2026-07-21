import asyncio

from pydantic import BaseModel, Field

from app.skills.subscription import analyze
from app.tools.base import Tool, Safety


class SubParams(BaseModel):
    source: str = Field(description="URL подписки (http/https) или текст со ссылками vless://, vmess://, trojan://, ss://, hysteria2://")
    no_net: bool = Field(default=False, description="не резолвить DNS и не ходить в ip-api (быстро, без ASN)")


async def sub_report(source: str, no_net: bool = False) -> dict:
    try:
        return {"markdown": await asyncio.to_thread(analyze.report, source, no_net)}
    except ValueError as e:
        return {"error": str(e)}


def build_tools() -> list[Tool]:
    return [
        Tool(
            "sub_report",
            "Parse a VPN subscription URL or raw proxy links (vless/vmess/trojan/ss/hysteria2/tuic) "
            "into a markdown report: servers, domains, resolved IPs, hosting provider and ASN, ports, "
            "protocols, transports, TLS/REALITY fingerprints, balancers and routing rules. "
            "Downloads the subscription itself (rotates client User-Agents). Safe, read-only, "
            "returns the whole report in one call — never fetch the subscription any other way.",
            SubParams,
            sub_report,
            Safety.SAFE,
        ),
    ]
