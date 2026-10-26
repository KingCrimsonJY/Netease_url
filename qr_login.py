"""命令行扫码登录：在终端显示二维码，扫码确认后把 cookie 写入 COOKIE_FILE。

用法:
    python qr_login.py login    扫码登录
    python qr_login.py status   查看登录状态与会员到期时间
    python qr_login.py logout   删除 cookie 文件（会先备份）
"""

from __future__ import annotations

import shutil
import sys
import time

import qrcode

from config import Settings
from main import QR_STATUS, account_summary, save_login_cookies
from music_api import CookieStore, NeteaseClient, NeteaseError, qr_login_url

POLL_INTERVAL = 2
TIMEOUT = 180


def make_client(store: CookieStore, settings: Settings) -> NeteaseClient:
    return NeteaseClient(
        store,
        os_name=settings.netease_os,
        appver=settings.netease_appver,
        device_id=settings.netease_device_id,
    )


def login(store: CookieStore, client: NeteaseClient) -> int:
    key = client.qr_unikey()
    qr = qrcode.QRCode(border=1)
    qr.add_data(qr_login_url(key))
    qr.print_ascii(invert=True)
    print("请使用网易云音乐 App 扫描上方二维码（3 分钟内有效）")
    last = None
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        result = client.qr_check(key)
        code = result["code"]
        state, message = QR_STATUS.get(code, ("error", result["message"] or f"code={code}"))
        if code != last:
            print(message)
            last = code
        if state == "success":
            if not result["cookies"].get("MUSIC_U"):
                print("登录成功但没有拿到 MUSIC_U")
                return 1
            save_login_cookies(store, result["cookies"])
            print(f"cookie 已保存到 {store.path}")
            return 0
        if state in ("expired", "error"):
            return 1
        time.sleep(POLL_INTERVAL)
    print("等待超时，请重新运行")
    return 1


def status(store: CookieStore, client: NeteaseClient) -> int:
    print(f"cookie 文件: {store.path}{'' if store.path.exists() else '（不存在）'}")
    if not store.get().get("MUSIC_U"):
        print("未登录：cookie 中没有 MUSIC_U")
        return 1
    info = account_summary(client)
    if not info["logged_in"]:
        print("cookie 已失效，请重新登录")
        return 1
    print(f"已登录: {info['nickname']}（{info['vip_name']}）")
    if info["expire_date"]:
        print(f"会员到期: {info['expire_date']}，剩余 {info['days_left']} 天")
    return 0


def logout(store: CookieStore) -> int:
    if not store.path.exists():
        print("没有 cookie 文件")
        return 0
    backup = store.path.with_name(store.path.name + ".bak")
    shutil.copy2(store.path, backup)
    store.clear()
    print(f"已删除 cookie（备份: {backup}）")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else "login"
    if command not in ("login", "status", "logout"):
        print(__doc__)
        return 0 if command in ("help", "-h", "--help") else 1
    settings = Settings.from_env()
    store = CookieStore(settings.cookie_file)
    client = make_client(store, settings)
    try:
        if command == "login":
            return login(store, client)
        if command == "status":
            return status(store, client)
        return logout(store)
    except NeteaseError as e:
        print(f"失败: {e.message}")
        return 1
    except KeyboardInterrupt:
        print("\n已取消")
        return 1


if __name__ == "__main__":
    sys.exit(main())
