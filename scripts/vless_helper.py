#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import sys
from urllib.parse import parse_qs, unquote, urlparse


def mask_secret(val: str) -> str:
    """脱敏辅助函数，保护构建日志敏感信息"""
    if not val or len(val) <= 8:
        return "******"
    return f"{val[:4]}...{val[-4:]}"


def parse_vless(url_str: str) -> dict:
    """解析标准 VLESS URL 并生成兼容 sing-box 1.11+ 的 outbound 字典"""
    parsed = urlparse(url_str.strip())
    if parsed.scheme.lower() != "vless":
        raise ValueError("提供的链接不是有效的 vless:// 节点链接")

    uuid = parsed.username
    server = parsed.hostname
    port = parsed.port or 443

    # 解析 query 参数
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

    security = params.get("security", "none").lower()
    net_type = params.get("type", "tcp").lower()
    flow = params.get("flow", "")
    sni = params.get("sni") or params.get("peer") or ""
    fingerprint = params.get("fp", "chrome")
    alpn_str = params.get("alpn", "")
    alpn = [x.strip() for x in alpn_str.split(",") if x.strip()] if alpn_str else []

    # 基础 Outbound 结构
    outbound = {
        "type": "vless",
        "tag": "proxy",
        "server": server,
        "server_port": int(port),
        "uuid": uuid,
    }

    # 1. 传输层处理：WS 模式下强制剔除 flow（解决协议冲突）
    if net_type == "ws":
        flow = ""
        ws_path = unquote(params.get("path", "/"))
        ws_host = params.get("host", sni or server)
        outbound["transport"] = {
            "type": "ws",
            "path": ws_path,
            "headers": {"Host": ws_host},
            "max_early_data": 0,
            "early_data_header_name": "Sec-WebSocket-Protocol",
        }
    elif net_type in ("grpc", "gun"):
        service_name = params.get("serviceName", "")
        outbound["transport"] = {
            "type": "grpc",
            "service_name": service_name,
        }
    elif net_type == "httpupgrade":
        outbound["transport"] = {
            "type": "httpupgrade",
            "path": unquote(params.get("path", "/")),
            "host": params.get("host", sni or server),
        }

    # 2. TLS / Reality / Flow 处理
    if security in ("tls", "reality"):
        tls_config = {
            "enabled": True,
            "server_name": sni if sni else server,
            "utls": {
                "enabled": True,
                "fingerprint": fingerprint,
            },
        }

        if alpn:
            tls_config["alpn"] = alpn

        if security == "reality":
            pbk = params.get("pbk", "")
            sid = params.get("sid", "")
            tls_config["reality"] = {
                "enabled": True,
                "public_key": pbk,
                "short_id": sid,
            }

        if flow and net_type in ("tcp", ""):
            outbound["flow"] = flow

        outbound["tls"] = tls_config

    return outbound


def build_singbox_config(outbound: dict) -> dict:
    """构建完整的 sing-box 运行配置"""
    return {
        "log": {
            "level": "info",
            "timestamp": True,
        },
        "inbounds": [
            {
                "type": "socks",
                "tag": "socks-in",
                "listen": "127.0.0.1",
                "listen_port": 10808,
            },
            {
                "type": "http",
                "tag": "http-in",
                "listen": "127.0.0.1",
                "listen_port": 10809,
            },
        ],
        "outbounds": [
            outbound,
            {"type": "direct", "tag": "direct"},
            {"type": "block", "tag": "block"},
        ],
        "route": {
            "final": "proxy",
            "rules": [
                {"inbound": ["socks-in", "http-in"], "outbound": "proxy"},
            ],
        },
    }


def main():
    node_link = (
        os.getenv("NODE_LINK")
        or os.getenv("VLESS_NODE")
        or os.getenv("WEIRDHOST_PROXY")
        or os.getenv("PROXY_NODE")
        or ""
    ).strip()

    if not node_link:
        print("[vless_helper] 未检测到任何节点配置环境变量，跳过代理生成。")
        return

    if not node_link.startswith("vless://"):
        print("[vless_helper] 检测到代理链接，但非 vless:// 协议，跳过处理。")
        return

    print("[vless_helper] 检测到 VLESS 节点，正在解析...")
    try:
        outbound = parse_vless(node_link)
    except Exception as e:
        print(f"[vless_helper] 节点解析失败: {e}")
        sys.exit(1)

    singbox_config = build_singbox_config(outbound)

    config_path = "/tmp/singbox.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(singbox_config, f, indent=2, ensure_ascii=False)

    print(f"[vless_helper] 已生成 {config_path}")

    # 日志脱敏
    safe_outbound = json.loads(json.dumps(outbound))
    if "uuid" in safe_outbound:
        safe_outbound["uuid"] = mask_secret(safe_outbound["uuid"])
    if "tls" in safe_outbound and "reality" in safe_outbound["tls"]:
        safe_outbound["tls"]["reality"]["public_key"] = mask_secret(
            safe_outbound["tls"]["reality"].get("public_key", "")
        )

    print("[vless_helper] 生效出站配置概要:")
    print(json.dumps(safe_outbound, indent=2, ensure_ascii=False))

    # 写入环境变量（必须注入 NO_PROXY 防止误拦截 ChromeDriver）
    github_env = os.getenv("GITHUB_ENV")
    if github_env and os.path.exists(github_env):
        with open(github_env, "a", encoding="utf-8") as f:
            f.write("all_proxy=socks5://127.0.0.1:10808\n")
            f.write("ALL_PROXY=socks5://127.0.0.1:10808\n")
            f.write("http_proxy=http://127.0.0.1:10809\n")
            f.write("https_proxy=http://127.0.0.1:10809\n")
            f.write("HTTP_PROXY=http://127.0.0.1:10809\n")
            f.write("HTTPS_PROXY=http://127.0.0.1:10809\n")
            f.write("no_proxy=localhost,127.0.0.1,::1\n")
            f.write("NO_PROXY=localhost,127.0.0.1,::1\n")
            f.write("WEIRDHOST_PROXY=socks5://127.0.0.1:10808\n")
        print("[vless_helper] 已将本地代理及 NO_PROXY 写入 $GITHUB_ENV")


if __name__ == "__main__":
    main()
