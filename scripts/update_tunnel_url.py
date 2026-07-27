"""把 tunnel 公開網址寫進 .env 的 BASE_URL（支援 cloudflared 與 ngrok）。

用法：
    # cloudflared（把啟動訊息裡的公開網址帶進來）：
    uv run python scripts/update_tunnel_url.py https://xxxx.trycloudflare.com

    # ngrok（不帶參數，自動從本機 API 抓）：
    uv run python scripts/update_tunnel_url.py

兩家金流的回傳網址都是逐筆帶在建單參數中，因此換網址只需要更新 BASE_URL
並重啟 server，不用動金流商後台設定。
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

NGROK_API = "http://127.0.0.1:4040/api/tunnels"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def fetch_ngrok_url() -> str:
    """回傳 ngrok 第一個 https tunnel 的公開網址。"""
    try:
        with urllib.request.urlopen(NGROK_API, timeout=5) as response:
            tunnels = json.load(response).get("tunnels", [])
    except OSError as exc:
        raise SystemExit(
            f"連不到 ngrok API（{NGROK_API}）：{exc}\n"
            "請先啟動 tunnel，二選一：\n"
            "  cloudflared tunnel --url http://localhost:8000   （再把網址帶給本腳本）\n"
            "  ngrok http 8000                                  （本腳本會自動抓）"
        ) from exc
    for tunnel in tunnels:
        url = tunnel.get("public_url", "")
        if url.startswith("https://"):  # 金流商僅接受 443/80；一律用 https
            return url
    raise SystemExit("ngrok 沒有 https tunnel（請用 ngrok http <port> 啟動）")


def update_env(public_url: str) -> None:
    if not ENV_PATH.exists():
        raise SystemExit(f"找不到 {ENV_PATH}（請先從 .env.example 建立）")
    content = ENV_PATH.read_text(encoding="utf-8")
    new_content, count = re.subn(
        r"(?m)^BASE_URL=.*$", f"BASE_URL={public_url}", content
    )
    if count == 0:
        new_content = content.rstrip("\n") + f"\nBASE_URL={public_url}\n"
    ENV_PATH.write_text(new_content, encoding="utf-8")


def main() -> None:
    if len(sys.argv) > 1:
        public_url = sys.argv[1].strip().rstrip("/")
        if not public_url.startswith("https://"):
            raise SystemExit(f"公開網址必須是 https：{public_url}")
    else:
        public_url = fetch_ngrok_url()
    update_env(public_url)
    print(f"BASE_URL 已更新為 {public_url}")
    print(
        "請重啟 server 讓設定生效："
        "uv run python -m uvicorn twpay_checkout.main:app"
    )


if __name__ == "__main__":
    sys.exit(main())
