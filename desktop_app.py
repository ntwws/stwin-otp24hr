from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import ctypes
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import winsound
import tkinter as tk
import customtkinter as ctk
from tkinter import messagebox, simpledialog, ttk
from datetime import datetime
from updater import UpdateManager


API_URL = "https://hero-sms.com/stubs/handler_api.php"
COUNTRY = 52
SERVICE = "me"
POLL_MS = 5000
FX_URL = "https://api.frankfurter.dev/v2/rate/USD/THB?providers=BOT"
APP_VERSION = "1.0.32"
UPDATE_MANIFEST_URL = "https://api.github.com/repos/ntwws/stwin-otp24hr/contents/update.json?ref=main"


def resource_path(filename: str) -> str:
    """Return a bundled resource path when running from a PyInstaller EXE."""
    base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, filename)


def cloud_api_url() -> str:
    value = os.environ.get("HERO_CLOUD_API_URL", "").strip()
    candidates = [os.path.join(os.path.dirname(sys.executable), "cloud_config.json"), resource_path("cloud_config.json")]
    if not value:
        for path in candidates:
            try:
                with open(path, encoding="utf-8") as stream: value = str(json.load(stream).get("api_url", "")).strip()
                if value: break
            except (OSError, ValueError, TypeError): pass
    return value.rstrip("/")


CLOUD_API_URL = cloud_api_url()


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class CredentialStore:
    """Encrypt settings with Windows DPAPI, scoped to the current Windows user."""
    def __init__(self):
        folder = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "HeroLineTH")
        self.path = os.path.join(folder, "credentials.dat")

    @staticmethod
    def _blob(data):
        buffer = ctypes.create_string_buffer(data)
        return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer

    def save(self, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        source, keepalive = self._blob(raw); target = _DataBlob()
        if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), ctypes.c_wchar_p("HeroLineTH"), None, None, None, 1, ctypes.byref(target)):
            raise OSError("ไม่สามารถเข้ารหัสการตั้งค่าได้")
        try: encrypted = ctypes.string_at(target.pbData, target.cbData)
        finally: ctypes.windll.kernel32.LocalFree(target.pbData)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as stream: stream.write(encrypted)

    def load(self):
        try:
            with open(self.path, "rb") as stream: encrypted = stream.read()
            source, keepalive = self._blob(encrypted); target = _DataBlob()
            if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
                return {}
            try: raw = ctypes.string_at(target.pbData, target.cbData)
            finally: ctypes.windll.kernel32.LocalFree(target.pbData)
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError): return {}

    def clear(self):
        try: os.remove(self.path)
        except OSError: pass


ERRORS = {
    "BAD_KEY": "API key ไม่ถูกต้อง",
    "NO_KEY": "กรุณากรอก API key",
    "NO_NUMBERS": "ขณะนี้ไม่มีหมายเลข LINE ประเทศไทย",
    "NO_BALANCE": "ยอดเงินไม่เพียงพอ",
    "BAD_SERVICE": "ไม่พบบริการ LINE ประเทศไทย",
    "SERVER_ERROR": "ขัดข้องชั่วคราว กรุณาลองใหม่",
    "STATUS_WAIT_CODE": "กำลังรอ SMS…",
    "STATUS_WAIT_RETRY": "กำลังรอ SMS ใหม่…",
    "STATUS_CANCEL": "รายการถูกยกเลิกแล้ว",
}


class HeroError(Exception):
    pass


class CloudClient:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")
        self.token = None

    def request(self, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "HeroLineTH-Windows/2.0"}
        if self.token: headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try: detail = json.loads(exc.read().decode("utf-8")).get("error")
            except Exception: detail = None
            raise HeroError(detail or f"Cloud API ตอบกลับ HTTP {exc.code}") from exc

    def login(self, username, password):
        result = self.request("/auth/login", "POST", {"username": username, "password": password})
        self.token = result["token"]
        return result


def _is_positive_number(value):
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


class HeroClient:
    def __init__(self, api_key: str = "", cloud=None):
        self.api_key = api_key.strip()
        self.cloud = cloud

    def request(self, action: str, **params):
        if self.cloud:
            body = str(self.cloud.request("/hero/request", "POST", {"action": action, "params": params})["raw"]).strip()
        else:
            if len(self.api_key) < 8:
                raise HeroError("กรุณากรอก API key ให้ถูกต้อง")
            query = {"api_key": self.api_key, "action": action, **params}
            url = API_URL + "?" + urllib.parse.urlencode(query)
            req = urllib.request.Request(url, headers={"User-Agent": "HeroLineTH-Windows/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=25) as response:
                    body = response.read().decode("utf-8", errors="replace").strip()
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace").strip()
                try:
                    error_data = json.loads(body)
                    if isinstance(error_data, dict) and error_data.get("title"):
                        title = str(error_data["title"])
                        raise HeroError(ERRORS.get(title, title)) from exc
                except json.JSONDecodeError:
                    pass
                raise HeroError(body or f"HeroSMS ตอบกลับ HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                raise HeroError(f"เชื่อมต่อ HeroSMS ไม่สำเร็จ: {exc}") from exc
        if body in ERRORS:
            raise HeroError(ERRORS[body])
        if body.startswith(("BAD_", "ERROR", "SERVER_ERROR", "BANNED")):
            raise HeroError(body)
        try:
            data = json.loads(body)
            if isinstance(data, dict) and data.get("title"):
                raise HeroError(ERRORS.get(data["title"], str(data["title"])))
            return data
        except json.JSONDecodeError:
            return body

    def balance(self) -> float:
        value = self.request("getBalance")
        if not isinstance(value, str) or not value.startswith("ACCESS_BALANCE:"):
            raise HeroError(str(value))
        return float(value.split(":", 1)[1])

    def price(self):
        offers = self.offers()
        if offers:
            return offers[0][0], sum(count for _, count in offers)
        data = self.request("getPrices", country=COUNTRY, service=SERVICE)
        entry = data.get(str(COUNTRY), {}).get(SERVICE) if isinstance(data, dict) else None
        if not entry:
            return None, 0
        return float(entry["cost"]), int(entry.get("count", 0))

    def offers(self):
        """Return selectable price tiers as [(price, count)], cheapest first."""
        for action in ("getPricesExtended", "getFreePrices", "getPricesV2"):
            try:
                data = self.request(action, country=COUNTRY, service=SERVICE,
                                    freePrice="true")
                root = data.get("data", data) if isinstance(data, dict) else {}
                entry = root.get(str(COUNTRY), {}).get(SERVICE, {}) if isinstance(root, dict) else {}
                price_map = {}
                if isinstance(entry, dict):
                    price_map = (entry.get("freePriceMap") or entry.get("prices") or
                                 entry.get("priceMap") or entry.get("price_map") or {})
                    if not price_map:
                        numeric_keys = {k: v for k, v in entry.items()
                                        if _is_positive_number(k)}
                        price_map = numeric_keys
                rows = []
                for price, value in price_map.items():
                    count = value.get("count", value.get("cnt", value.get("quantity", 0))) if isinstance(value, dict) else value
                    if float(price) > 0 and int(count) > 0:
                        rows.append((float(price), int(count)))
                if rows:
                    return sorted(rows)
                if isinstance(entry, dict) and entry.get("cost") is not None:
                    count = int(entry.get("count", entry.get("cnt", 0)))
                    if count > 0:
                        return [(float(entry["cost"]), count)]
            except HeroError as exc:
                message = str(exc)
                if "BAD_ACTION" not in message and "BAD_REQUEST" not in message and "Method Not Found" not in message:
                    raise
        data = self.request("getPrices", country=COUNTRY, service=SERVICE)
        entry = data.get(str(COUNTRY), {}).get(SERVICE, {}) if isinstance(data, dict) else {}
        if isinstance(entry, dict) and entry.get("cost") is not None and int(entry.get("count", 0)) > 0:
            return [(float(entry["cost"]), int(entry["count"]))]
        return []

    @staticmethod
    def usd_thb():
        req = urllib.request.Request(FX_URL, headers={"User-Agent": "HeroLineTH-Windows/1.1"})
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
            return float(data["rate"]), str(data["date"])
        except Exception:
            return None, None

    def buy(self, accepted_price: float):
        offers = self.offers()
        selected = next(((price, count) for price, count in offers
                         if abs(price - accepted_price) <= 1e-9), None)
        if selected is None or selected[1] < 1:
            raise HeroError(ERRORS["NO_NUMBERS"])
        result = self.request("getNumber", country=COUNTRY, service=SERVICE,
                              maxPrice=accepted_price)
        if not isinstance(result, str) or not result.startswith("ACCESS_NUMBER:"):
            raise HeroError(ERRORS.get(str(result).split(":", 1)[0], str(result)))
        _, activation_id, phone = result.split(":", 2)
        return activation_id, phone, accepted_price


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        try:
            self.iconbitmap(resource_path("1.ico"))
        except tk.TclError:
            pass
        self.title("OTP24HR")
        window_width, window_height = 720, 680
        x = max(0, (self.winfo_screenwidth() - window_width) // 2)
        y = max(0, (self.winfo_screenheight() - window_height) // 2)
        self.geometry(f"{window_width}x{window_height}+{x}+{y}")
        self.resizable(False, False)
        self.configure(bg="#061321")
        self.client = None
        self.quote = None
        self.fx_rate = None
        self.offer_rows = []
        self.activation_id = None
        self.activation_price = None
        self.remaining_seconds = 0
        self.poll_job = None
        self.timer_job = None
        self.jobs = queue.Queue()
        self._build()
        self.after(100, self._drain_jobs)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TButton", font=("Segoe UI", 10), padding=(11, 7),
                        background="#173a5e", foreground="#eaf4ff", bordercolor="#315b82")
        style.map("TButton", background=[("active", "#245785"), ("disabled", "#172b40")],
                  foreground=[("disabled", "#71869b")])
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 8),
                        background="#1687d9", foreground="#ffffff", bordercolor="#42a5ee")
        style.map("Accent.TButton", background=[("active", "#2b9bea"), ("disabled", "#17344d")])
        style.configure("TEntry", fieldbackground="#0b1d30", foreground="#eaf4ff",
                        insertcolor="#ffffff", bordercolor="#315b82", padding=6)
        style.configure("Title.TLabel", font=("Segoe UI", 19, "bold"), background="#0b1f36", foreground="#eaf6ff")
        style.configure("Head.TLabel", font=("Segoe UI", 12, "bold"), background="#0b1f36", foreground="#dcecff")
        style.configure("Body.TLabel", font=("Segoe UI", 10), background="#0b1f36", foreground="#8fa9c2")
        style.configure("Value.TLabel", font=("Segoe UI", 15, "bold"), background="#102a46", foreground="#f2f8ff")
        style.configure("Treeview", background="#0d2239", fieldbackground="#0d2239",
                        foreground="#deedfb", rowheight=27, bordercolor="#294866")
        style.configure("Treeview.Heading", background="#173a5e", foreground="#eaf4ff",
                        font=("Segoe UI", 9, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", "#146aa6")], foreground=[("selected", "#ffffff")])
        style.map("Treeview.Heading", background=[("active", "#214d76")])
        style.configure("TSeparator", background="#294866")
        card = tk.Frame(self, bg="#0b1f36", padx=20, pady=12, highlightthickness=1,
                        highlightbackground="#294866")
        card.pack(fill="both", expand=True, padx=12, pady=10)
        ttk.Label(card, text="OTP24HR", style="Title.TLabel").pack(anchor="w")
        ttk.Label(card, text="ซื้อหมายเลขและรับ OTP ", style="Body.TLabel").pack(anchor="w", pady=(0, 7))
        keyrow = tk.Frame(card, bg="#0b1f36")
        keyrow.pack(fill="x")
        self.key_var = tk.StringVar()
        ttk.Entry(keyrow, textvariable=self.key_var, show="•", font=("Segoe UI", 10)).pack(side="left", fill="x", expand=True, ipady=5)
        ttk.Button(keyrow, text="เชื่อมต่อ", command=self.refresh, style="Accent.TButton").pack(side="left", padx=(9, 0))
        info = tk.Frame(card, bg="#102a46", padx=12, pady=8)
        info.pack(fill="x", pady=7)
        for col, (label, value) in enumerate((("บริการ", "LINE"), ("ประเทศ", "ไทย 🇹🇭"))):
            box = tk.Frame(info, bg="#102a46")
            box.grid(row=0, column=col, sticky="ew")
            ttk.Label(box, text=label, style="Body.TLabel", background="#102a46").pack(anchor="w")
            ttk.Label(box, text=value, style="Value.TLabel").pack(anchor="w")
        pricebox = tk.Frame(info, bg="#102a46")
        pricebox.grid(row=0, column=2, sticky="ew")
        ttk.Label(pricebox, text="ราคาปัจจุบัน", style="Body.TLabel", background="#102a46").pack(anchor="w")
        self.price_var = tk.StringVar(value="$ — USD")
        ttk.Label(pricebox, textvariable=self.price_var, style="Value.TLabel").pack(anchor="w")
        self.thb_var = tk.StringVar(value="≈ ฿ — THB")
        ttk.Label(pricebox, textvariable=self.thb_var, style="Value.TLabel", foreground="#078342").pack(anchor="w")
        self.stock_var = tk.StringVar(value="คงเหลือ — หมายเลข")
        ttk.Label(pricebox, textvariable=self.stock_var, style="Body.TLabel", background="#102a46").pack(anchor="w", pady=(4, 0))
        self.fx_var = tk.StringVar(value="USD/THB: —")
        ttk.Label(pricebox, textvariable=self.fx_var, style="Body.TLabel", background="#102a46").pack(anchor="w")
        for i in range(3): info.grid_columnconfigure(i, weight=1)
        offer_head = tk.Frame(card, bg="#0b1f36")
        offer_head.pack(fill="x", pady=(3, 3))
        ttk.Label(offer_head, text="เลือกราคา", style="Head.TLabel").pack(side="left")
        ttk.Label(offer_head, text="คลิกแถวที่ต้องการก่อนซื้อ", style="Body.TLabel").pack(side="right")
        table_frame = tk.Frame(card, bg="#0b1f36", highlightthickness=1, highlightbackground="#294866")
        table_frame.pack(fill="x")
        self.offer_table = ttk.Treeview(table_frame, columns=("price_usd", "price_thb", "stock"),
                                        show="headings", height=2, selectmode="browse")
        self.offer_table.heading("price_usd", text="ราคา USD")
        self.offer_table.heading("price_thb", text="ราคา THB")
        self.offer_table.heading("stock", text="จำนวนคงเหลือ")
        self.offer_table.column("price_usd", width=145, anchor="center")
        self.offer_table.column("price_thb", width=145, anchor="center")
        self.offer_table.column("stock", width=145, anchor="center")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.offer_table.yview)
        self.offer_table.configure(yscrollcommand=scroll.set)
        self.offer_table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.offer_table.bind("<<TreeviewSelect>>", self._offer_selected)
        actions = tk.Frame(card, bg="#0b1f36")
        actions.pack(fill="x", pady=(4, 0))
        ttk.Button(actions, text="อัปเดตราคา", command=self.refresh).pack(side="left")
        self.buy_btn = ttk.Button(actions, text="ซื้อหมายเลข LINE", command=self.buy, state="disabled", style="Accent.TButton")
        self.buy_btn.pack(side="left", padx=8)
        self.notice_var = tk.StringVar(value="กรอก API key แล้วกดเชื่อมต่อ")
        self.notice = tk.Label(card, textvariable=self.notice_var, bg="#0b1f36", fg="#8fa9c2",
                               font=("Segoe UI", 10), anchor="w", wraplength=520)
        self.notice.pack(fill="x", pady=(4, 2))
        ttk.Separator(card).pack(fill="x", pady=2)
        ttk.Label(card, text="รายการที่กำลังใช้งาน", style="Head.TLabel").pack(anchor="w", pady=(3, 3))
        self.phone_var = tk.StringVar(value="—")
        self.status_var = tk.StringVar(value="ยังไม่มีรายการ")
        active_frame = tk.Frame(card, bg="#0b1f36", highlightthickness=1, highlightbackground="#294866")
        active_frame.pack(fill="x")
        self.active_table = ttk.Treeview(active_frame,
            columns=("number", "cost", "time", "status", "code"),
            show="headings", height=1, selectmode="none")
        for column, title, width in (("number", "Number", 175), ("cost", "Cost", 90),
                                     ("time", "Time", 70), ("status", "Status", 145),
                                     ("code", "SMS Code", 120)):
            self.active_table.heading(column, text=title)
            self.active_table.column(column, width=width, anchor="center")
        self.active_table.pack(fill="x")
        self.active_table.bind("<Button-3>", self._copy_number)
        self.active_table.insert("", "end", iid="active",
                                 values=("—", "—", "20:00", "ยังไม่มีรายการ", "—"))
        order_actions = tk.Frame(card, bg="#0b1f36")
        order_actions.pack(fill="x", pady=(4, 0))
        self.poll_btn = ttk.Button(order_actions, text="ตรวจ OTP", command=self.poll, state="disabled")
        self.poll_btn.pack(side="left")
        self.complete_btn = ttk.Button(order_actions, text="เสร็จสิ้น", command=lambda: self.command("complete"), state="disabled")
        self.complete_btn.pack(side="left", padx=7)
        self.cancel_btn = ttk.Button(order_actions, text="ยกเลิก", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left")

    def _client(self):
        return HeroClient(self.key_var.get())

    def _copy_number(self, event=None):
        number = self.phone_var.get().strip()
        if not number or number == "—":
            self.notice_var.set("ยังไม่มีหมายเลขให้คัดลอก")
            self.notice.configure(fg="#ffba6b")
            return
        self.clipboard_clear()
        self.clipboard_append(number)
        self.update_idletasks()
        self.notice_var.set(f"คัดลอกหมายเลข {number} แล้ว")
        self.notice.configure(fg="#58d6ff")

    def _run(self, work, success=None):
        def runner():
            try: result = work()
            except Exception as exc: self.jobs.put((self._error, (str(exc),)))
            else:
                if success: self.jobs.put((success, (result,)))
        threading.Thread(target=runner, daemon=True).start()

    def _drain_jobs(self):
        try:
            while True:
                fn, args = self.jobs.get_nowait()
                fn(*args)
        except queue.Empty: pass
        self.after(100, self._drain_jobs)

    def _error(self, message):
        self.notice_var.set(message)
        self.notice.configure(fg="#ff7b7b")
        self.buy_btn.configure(state="disabled" if self.quote is None else "normal")

    def refresh(self):
        self.notice.configure(fg="#8fa9c2")
        self.notice_var.set("กำลังอัปเดตราคาและยอดเงิน…")
        self.buy_btn.configure(state="disabled")
        client = self._client()
        self._run(lambda: (client.offers(), client.balance(), client.usd_thb()), self._refreshed)

    def _refreshed(self, result):
        offers, balance, (fx_rate, fx_date) = result
        price = offers[0][0] if offers else None
        count = sum(item[1] for item in offers)
        self.quote = price
        self.offer_rows = offers
        self.fx_rate = fx_rate
        self.price_var.set("ไม่มีสินค้า" if price is None else f"$ {price:.4f} USD")
        self.thb_var.set("≈ ฿ — THB" if price is None or fx_rate is None else f"≈ ฿ {price * fx_rate:.2f} THB")
        self.stock_var.set(f"คงเหลือ {count:,} หมายเลข")
        self.fx_var.set("USD/THB ใช้งานไม่ได้" if fx_rate is None else f"1 USD = ฿{fx_rate:.4f} ({fx_date})")
        for item in self.offer_table.get_children():
            self.offer_table.delete(item)
        for index, (offer_price, offer_count) in enumerate(offers):
            thb = "—" if fx_rate is None else f"฿ {offer_price * fx_rate:.2f}"
            self.offer_table.insert("", "end", iid=str(index),
                                    values=(f"$ {offer_price:.4f}", thb, f"{offer_count:,} pcs"))
        if offers:
            self.offer_table.selection_set("0")
            self.offer_table.focus("0")
        now = datetime.now().strftime("%H:%M:%S")
        balance_thb = "฿ — THB" if fx_rate is None else f"≈ ฿ {balance * fx_rate:,.2f} THB"
        self.notice_var.set(
            f"พร้อมใช้งาน • ยอดคงเหลือ ${balance:.4f} USD ({balance_thb}) • อัปเดต {now}"
        )
        self.notice.configure(fg="#8fa9c2")
        self.buy_btn.configure(state="normal" if price is not None and count > 0 else "disabled")

    def _offer_selected(self, _event=None):
        selected = self.offer_table.selection()
        if not selected:
            return
        index = int(selected[0])
        if index >= len(self.offer_rows):
            return
        self.quote = self.offer_rows[index][0]
        self.price_var.set(f"$ {self.quote:.4f} USD")
        self.thb_var.set("≈ ฿ — THB" if self.fx_rate is None else f"≈ ฿ {self.quote * self.fx_rate:.2f} THB")
        self.buy_btn.configure(state="normal")

    def buy(self):
        if self.quote is None: return
        if not messagebox.askyesno("ยืนยันการซื้อ", f"ซื้อหมายเลข LINE ประเทศไทย ราคา ${self.quote:.4f} ?"):
            return
        self.buy_btn.configure(state="disabled")
        self.notice_var.set("กำลังซื้อหมายเลข…")
        client, price = self._client(), self.quote
        self._run(lambda: client.buy(price), self._bought)

    def _bought(self, result):
        activation_id, phone, price = result
        self.activation_id = activation_id
        self.activation_price = price
        self.remaining_seconds = 20 * 60
        self.phone_var.set("+" + phone.lstrip("+"))
        self.status_var.set("กำลังรอ SMS…")
        self.notice_var.set(f"ซื้อสำเร็จ ราคา ${price:.4f}")
        self._sync_activation_row()
        self._start_timer()
        for btn in (self.poll_btn, self.complete_btn, self.cancel_btn): btn.configure(state="normal")
        self.command("ready")
        self._schedule_poll()

    def poll(self):
        if not self.activation_id: return
        client, aid = self._client(), self.activation_id
        self._run(lambda: client.request("getStatus", id=aid), self._polled)

    def _polled(self, raw):
        raw = str(raw)
        state, _, value = raw.partition(":")
        if state == "STATUS_OK":
            self.status_var.set("OTP: " + value)
            self._sync_activation_row(value)
            self._stop_poll()
        else:
            self.status_var.set(ERRORS.get(state, raw))
            self._sync_activation_row()
            if state == "STATUS_CANCEL": self._stop_poll()

    def command(self, name):
        if not self.activation_id: return
        status = {"ready": 1, "complete": 6, "cancel": 8}[name]
        client, aid = self._client(), self.activation_id
        self._run(lambda: client.request("setStatus", id=aid, status=status),
                  lambda _: self._commanded(name))

    def _commanded(self, name):
        if name == "complete": self.status_var.set("เสร็จสิ้นแล้ว")
        elif name == "cancel": self.status_var.set("ยกเลิกแล้ว")
        self._sync_activation_row()
        if name in {"complete", "cancel"}:
            self._stop_poll()
            self._stop_timer()
            for btn in (self.poll_btn, self.complete_btn, self.cancel_btn): btn.configure(state="disabled")

    def cancel(self):
        if messagebox.askyesno("ยืนยันการยกเลิก", "ต้องการยกเลิกหมายเลขนี้หรือไม่?"):
            self.command("cancel")

    def _schedule_poll(self):
        self._stop_poll()
        self.poll_job = self.after(POLL_MS, self._auto_poll)

    def _auto_poll(self):
        self.poll()
        if self.activation_id: self.poll_job = self.after(POLL_MS, self._auto_poll)

    def _sync_activation_row(self, code=""):
        minutes, seconds = divmod(max(0, self.remaining_seconds), 60)
        cost = "—" if self.activation_price is None else f"${self.activation_price:.4f}"
        status = self.status_var.get()
        if status.startswith("OTP:") and not code:
            code = status.split(":", 1)[1].strip()
        self.active_table.item("active", values=(self.phone_var.get(), cost,
            f"{minutes:02d}:{seconds:02d}", status, code or "—"))

    def _start_timer(self):
        self._stop_timer()
        self._tick_timer()

    def _tick_timer(self):
        self._sync_activation_row()
        if self.remaining_seconds > 0:
            self.remaining_seconds -= 1
            self.timer_job = self.after(1000, self._tick_timer)
        else:
            self.timer_job = None

    def _stop_timer(self):
        if self.timer_job:
            self.after_cancel(self.timer_job)
            self.timer_job = None

    def _stop_poll(self):
        if self.poll_job:
            self.after_cancel(self.poll_job)
            self.poll_job = None

    def _close(self):
        self._stop_poll()
        self._stop_timer()
        self.destroy()


class OrderListView(ctk.CTkFrame):
    """A rounded, expandable OTP list with the Treeview API used by the app."""

    COLUMNS = ((0, 42, 0), (1, 210, 3), (2, 120, 2), (3, 190, 3), (4, 110, 2), (5, 82, 1))

    def __init__(self, master, on_action=None, on_more=None, on_copy_number=None, **kwargs):
        super().__init__(master, fg_color="#0b1120", corner_radius=9,
                         border_width=1, border_color="#2a3451", **kwargs)
        self._ids = []
        self._values = {}
        self._selected = None
        self._row_widgets = {}
        self._render_job = None
        # CTkFont construction is relatively expensive (it consults the
        # scaling/font manager every time).  Rows are rebuilt when changing
        # filters/pages, so reusing a handful of immutable font objects keeps
        # those rebuilds responsive even with a full 25-row page.
        self._font_header = ctk.CTkFont("Leelawadee UI", 13, "bold")
        self._font_body = ctk.CTkFont("Segoe UI", 13)
        self._font_body_bold = ctk.CTkFont("Segoe UI", 13, "bold")
        self._font_status = ctk.CTkFont("Leelawadee UI", 12)
        self._font_radio = ctk.CTkFont("Segoe UI Symbol", 18)
        self._font_action = ctk.CTkFont("Leelawadee UI", 13, "bold")
        self.on_action = on_action
        self.on_more = on_more
        self.on_copy_number = on_copy_number
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, height=48, fg_color="#191d3a", corner_radius=8)
        header.grid(row=0, column=0, sticky="ew", padx=1, pady=1)
        header.grid_propagate(False)
        self._configure_columns(header)
        for column, text in ((1, "หมายเลข"), (2, "เวลา"), (3, "สถานะ"),
                             (4, "OTP"), (5, "การทำงาน")):
            ctk.CTkLabel(header, text=text, text_color="#c1c8dd",
                         font=self._font_header).grid(
                             row=0, column=column, sticky="nsew", padx=5)

        self.body = ctk.CTkScrollableFrame(
            self, fg_color="#0b1120", corner_radius=0,
            scrollbar_button_color="#303a58", scrollbar_button_hover_color="#465271")
        self.body.grid(row=1, column=0, sticky="nsew", padx=1, pady=(0, 1))
        self.body.grid_columnconfigure(0, weight=1)

    def _configure_columns(self, frame):
        for column, minsize, weight in self.COLUMNS:
            frame.grid_columnconfigure(column, minsize=minsize, weight=weight)
        frame.grid_rowconfigure(0, weight=1)

    @staticmethod
    def _status_style(status):
        text = str(status)
        if "ได้รับ OTP" in text:
            return "#123b32", "#47d6a2"
        if "หมดเวลา" in text or "ยกเลิก" in text:
            return "#252a3b", "#aab2c8"
        if "ตรวจไม่สำเร็จ" in text or "⚠" in text:
            return "#401d28", "#ff7a8b"
        if "รอรับ" in text or "ขอ OTP" in text:
            return "#102e4b", "#56b6ff"
        return "#3b2c0d", "#f6ad2f"

    def _select(self, iid):
        if iid not in self._ids:
            return
        if iid == self._selected:
            return
        previous = self._selected
        self._selected = iid
        self._apply_selection_style(previous, False)
        self._apply_selection_style(iid, True)
        self.event_generate("<<TreeviewSelect>>")

    def _action(self, iid, name):
        self._select(iid)
        if self.on_action:
            self.on_action(name)

    def _more(self, iid, widget):
        x_root = widget.winfo_rootx() + max(20, widget.winfo_width() - 70)
        y_root = widget.winfo_rooty() + 42
        self._select(iid)
        if self.on_more:
            self.on_more(iid, x_root, y_root)

    def _copy_number(self, iid):
        self._select(iid)
        if self.on_copy_number:
            self.on_copy_number(iid)

    def _context_menu(self, iid, event):
        self._select(iid)
        if self.on_more:
            self.on_more(iid, event.x_root, event.y_root)

    def _copy(self, value):
        if not value or value == "—":
            return
        self.clipboard_clear(); self.clipboard_append(value)

    def _make_action_bar(self, row, iid):
        bar = ctk.CTkFrame(row, height=48, fg_color="#10172a", corner_radius=24,
                           border_width=1, border_color="#3a4564")
        bar.grid(row=1, column=1, columnspan=5, sticky="ew", padx=(20, 24), pady=(0, 13))
        bar.grid_propagate(False)
        bar.grid_rowconfigure(0, weight=1)
        for index in range(7):
            bar.grid_columnconfigure(index, weight=1 if index % 2 == 0 else 0)
        actions = (("↻   ตรวจ OTP", "poll", "#dfe4f5", "#222b44"),
                   ("▤   ขอ OTP ซ้ำ", "resend", "#dfe4f5", "#222b44"),
                   ("✓   เสร็จสิ้น", "complete", "#dfe4f5", "#222b44"),
                   ("⊗   ยกเลิก", "cancel", "#ff646f", "#401d28"))
        for index, (text, name, color, hover) in enumerate(actions):
            ctk.CTkButton(bar, text=text, height=36, fg_color="transparent", hover_color=hover,
                          text_color=color, corner_radius=8, border_width=1 if name == "cancel" else 0,
                          border_color="#ef4653" if name == "cancel" else "#10172a",
                          font=self._font_action,
                          command=lambda n=name: self._action(iid, n)).grid(
                              row=0, column=index * 2, sticky="ew", padx=8, pady=6)
            if index < 3:
                ctk.CTkFrame(bar, width=1, height=28, fg_color="#35405e").grid(
                    row=0, column=index * 2 + 1, pady=10)
        return bar

    def _apply_selection_style(self, iid, selected):
        widgets = self._row_widgets.get(iid)
        if not widgets:
            return
        row = widgets["row"]
        row.configure(height=130 if selected else 61,
                      fg_color="#121a33" if selected else "#0b1120",
                      corner_radius=9 if selected else 0,
                      border_width=1 if selected else 0)
        row.grid_rowconfigure(1, minsize=61 if selected else 0)
        widgets["radio"].configure(text="●" if selected else "○",
                                   text_color="#8b5cf6" if selected else "#9ba6c2")
        widgets["flag"].configure(bg="#121a33" if selected else "#0b1120")
        action_bar = widgets.get("action_bar")
        if selected and action_bar is None:
            widgets["action_bar"] = self._make_action_bar(row, iid)
        elif selected:
            action_bar.grid()
        elif action_bar is not None:
            # Keep the controls cached. Recreating four CTk buttons on every
            # click was the remaining source of visible selection latency.
            action_bar.grid_remove()

    def _render(self):
        if self._render_job is not None:
            try: self.after_cancel(self._render_job)
            except tk.TclError: pass
            self._render_job = None
        for widget in self.body.winfo_children():
            widget.destroy()
        self._row_widgets = {}
        for row_index, iid in enumerate(self._ids):
            values = list(self._values.get(iid, ()))
            values += ["—"] * (5 - len(values))
            phone, remaining, status, code = values[:4]
            selected = iid == self._selected
            row = ctk.CTkFrame(
                self.body, height=130 if selected else 61,
                fg_color="#121a33" if selected else "#0b1120",
                corner_radius=9 if selected else 0,
                border_width=1 if selected else 0,
                border_color="#8b5cf6")
            row.grid(row=row_index, column=0, sticky="ew", pady=(0, 1))
            row.grid_propagate(False); self._configure_columns(row)
            if selected:
                row.grid_rowconfigure(1, minsize=61)

            radio = ctk.CTkButton(
                row, text="●" if selected else "○", width=28, height=28,
                fg_color="transparent", hover_color="#242d49",
                text_color="#8b5cf6" if selected else "#9ba6c2",
                font=self._font_radio,
                command=lambda value=iid: self._select(value))
            radio.grid(row=0, column=0, padx=(10, 2))

            number = ctk.CTkFrame(row, fg_color="transparent")
            number.grid(row=0, column=1, sticky="w", padx=6)
            flag_bg = "#121a33" if selected else "#0b1120"
            flag = tk.Canvas(number, width=24, height=16, bg=flag_bg, highlightthickness=0)
            flag.pack(side="left", padx=(1, 3))
            for y, color in ((1, "#ef3340"), (4, "#ffffff"), (6, "#2d2a8c"),
                             (10, "#ffffff"), (12, "#ef3340")):
                height = 5 if y == 6 else (3 if y in (1, 12) else 2)
                flag.create_rectangle(1, y, 23, y + height, fill=color, outline=color)
            phone_label = ctk.CTkLabel(number, text=str(phone), text_color="#f1f3f9",
                                       font=self._font_body)
            phone_label.pack(side="left", padx=(7, 0))
            phone_label.bind("<Button-1>", lambda _e, value=iid: self._copy_number(value))

            time_label = ctk.CTkLabel(row, text=str(remaining), text_color="#f1f3f9",
                                      font=self._font_body)
            time_label.grid(row=0, column=2)
            status_bg, status_fg = self._status_style(status)
            status_label = ctk.CTkLabel(row, text=str(status), fg_color=status_bg, text_color=status_fg,
                                        corner_radius=6, height=31,
                                        font=self._font_status)
            status_label.grid(row=0, column=3, padx=12)
            otp_box = ctk.CTkFrame(row, fg_color="transparent")
            otp_box.grid(row=0, column=4)
            code_label = ctk.CTkLabel(otp_box, text=str(code), text_color="#f1f3f9",
                                      font=self._font_body_bold)
            code_label.pack(side="left")
            if code not in (None, "", "—"):
                ctk.CTkButton(otp_box, text="▣", width=28, height=28, fg_color="#30235f",
                              hover_color="#49347f", text_color="#d9ceff", corner_radius=6,
                              command=lambda value=code: self._copy(value)).pack(side="left", padx=(7, 0))
            more = ctk.CTkButton(row, text="•••", width=42, height=32, fg_color="transparent",
                                 hover_color="#242d49", text_color="#aeb7d0",
                                 command=lambda value=iid, widget=row: self._more(value, widget))
            more.grid(row=0, column=5)
            for target in (row, number):
                target.bind("<Button-1>", lambda _e, value=iid: self._select(value))
            for target in (row, number, phone_label, flag, time_label, status_label, code_label):
                target.bind("<Button-3>", lambda event, value=iid: self._context_menu(value, event))
            action_bar = self._make_action_bar(row, iid) if selected else None
            self._row_widgets[iid] = {
                "time": time_label, "status": status_label, "code": code_label,
                "has_copy": code not in (None, "", "—"), "row": row,
                "radio": radio, "flag": flag, "action_bar": action_bar
            }

    def _schedule_render(self):
        if self._render_job is None:
            self._render_job = self.after_idle(self._render)

    def insert(self, _parent, _index, iid=None, values=()):
        iid = str(iid if iid is not None else len(self._ids))
        if iid not in self._ids:
            self._ids.append(iid)
        if values:
            self._values[iid] = tuple(values)
        else:
            self._values.setdefault(iid, ())
        self._schedule_render()
        return iid

    def item(self, iid, option=None, **kwargs):
        iid = str(iid)
        if "values" in kwargs:
            new_values = tuple(kwargs["values"])
            old_values = self._values.get(iid, ())
            self._values[iid] = new_values
            if iid not in self._ids:
                self._ids.append(iid)
            widgets = self._row_widgets.get(iid)
            if widgets and len(new_values) >= 4:
                _, remaining, status, code = new_values[:4]
                has_copy = code not in (None, "", "—")
                if has_copy != widgets["has_copy"]:
                    self._schedule_render()
                else:
                    if len(old_values) < 2 or old_values[1] != remaining:
                        widgets["time"].configure(text=str(remaining))
                    if len(old_values) < 3 or old_values[2] != status:
                        status_bg, status_fg = self._status_style(status)
                        widgets["status"].configure(text=str(status), fg_color=status_bg, text_color=status_fg)
                    if len(old_values) < 4 or old_values[3] != code:
                        widgets["code"].configure(text=str(code))
            else:
                self._schedule_render()
        values = self._values.get(iid, ())
        if option == "values":
            return values
        return {"values": values}

    def exists(self, iid):
        return str(iid) in self._ids

    def delete(self, iid):
        iid = str(iid)
        if iid in self._ids:
            self._ids.remove(iid)
        self._values.pop(iid, None)
        if self._selected == iid:
            self._selected = None
        self._schedule_render()

    def get_children(self, _item=None):
        return tuple(self._ids)

    def clear(self):
        self._ids.clear(); self._values.clear(); self._selected = None
        self._schedule_render()

    def selection(self):
        return (self._selected,) if self._selected in self._ids else ()

    def selection_set(self, iid):
        iid = str(iid)
        if iid in self._ids:
            self._select(iid)

    def focus(self, _iid=None):
        return self._selected

    def identify_row(self, y):
        for iid, widgets in self._row_widgets.items():
            widget = widgets["row"]
            if widget.winfo_y() <= y < widget.winfo_y() + widget.winfo_height():
                return iid
        return ""


class WebStyleApp(tk.Tk):
    """OTP24HR desktop client with a web-style, dark violet interface."""

    def __init__(self):
        super().__init__()
        # Build the main window while hidden so it never flashes behind Login.
        self.withdraw()
        self.title(f"OTP24HR by STWIN — v{APP_VERSION}")
        try:
            self.iconbitmap(resource_path("1.ico"))
        except tk.TclError:
            pass
        width, height = 1280, 800
        self.geometry(f"{width}x{height}+{max(0, (self.winfo_screenwidth()-width)//2)}+{max(0, (self.winfo_screenheight()-height)//2)}")
        self.minsize(1040, 680)
        self.resizable(True, True)
        self.configure(bg="#070510")
        self.jobs = queue.Queue()
        self.quote = self.fx_rate = None
        self.offer_rows = []
        self.orders = {}
        self.polling_ids = set()
        self.cloud_success_pending = set()
        # Prevent overlapping wallet requests when several activations are
        # completed in quick succession.  The API call is asynchronous; a
        # small guard keeps the UI responsive and avoids stale responses
        # racing each other and replacing the latest balance.
        self._balance_refresh_pending = False
        self.credential_store = CredentialStore()
        self.saved_settings = self.credential_store.load()
        self.cloud = CloudClient(CLOUD_API_URL) if CLOUD_API_URL else None
        self.cloud_user = None
        self.cloud_role = None
        self.monthly_success = 0
        self.monthly_purchased = 0
        self.daily_limit = 0
        self.daily_purchased = 0
        self.daily_success = 0
        self.first_success_today = None
        self.last_success_today = None
        self.latest_cycle_date = None
        self.first_success_latest_cycle = None
        self.last_success_latest_cycle = None
        self.estimated_24h_end = None
        self.server_now_bangkok = None
        self.history_page = 1
        self.history_page_size = 25
        self.history_total = 0
        self.history_counts = {"success": 0, "all": 0}
        self.history_page_ids = []
        self.history_loading = False
        self.history_search_job = None
        self.history_request_id = 0
        self.update_manager = UpdateManager(APP_VERSION, UPDATE_MANIFEST_URL)
        self.update_checked = False
        self.poll_job = self.timer_job = None
        self._save_orders_job = None
        self.data_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "HeroLineTH")
        self.orders_file = None
        self._build_ui()
        self.after(100, self._drain_jobs)
        if self.cloud:
            self.after(80, self._require_login)
        else:
            self.after(80, self._show_main_window)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui_legacy(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        palette = {
            "window": "#070b16", "sidebar": "#090e1c", "panel": "#10162a",
            "panel_2": "#141a31", "border": "#29324d", "accent": "#7c3aed",
            "accent_2": "#5b21b6", "text": "#f7f4ff", "muted": "#9da7c3",
            "success": "#38d39f", "danger": "#ef4444", "warning": "#f59e0b",
        }
        self.palette = palette
        self.configure(bg=palette["window"])
        style.configure("TEntry", fieldbackground="#0c1222", foreground=palette["text"],
                        insertcolor="#ffffff", padding=9, bordercolor=palette["border"])
        style.configure("TSpinbox", fieldbackground="#0c1222", foreground=palette["text"],
                        arrowcolor=palette["text"], padding=8, bordercolor=palette["border"])
        style.configure("TButton", font=("Segoe UI", 10, "bold"), padding=(14, 10),
                        background="#171e35", foreground=palette["text"], bordercolor="#35405f")
        style.map("TButton", background=[("active", "#222b48"), ("disabled", "#101526")],
                  foreground=[("disabled", "#59627a")])
        style.configure("Green.TButton", background=palette["accent"], foreground="#ffffff",
                        bordercolor="#9b6cff", padding=(20, 11))
        style.map("Green.TButton", background=[("active", "#8b5cf6"), ("disabled", "#2d2350")])
        style.configure("Danger.TButton", background="#321523", foreground="#ff7373",
                        bordercolor=palette["danger"])
        style.map("Danger.TButton", background=[("active", "#4b1825")])
        style.configure("Nav.TButton", font=("Segoe UI", 11), padding=(18, 13),
                        background=palette["sidebar"], foreground=palette["muted"],
                        borderwidth=0, anchor="w")
        style.map("Nav.TButton", background=[("active", "#151b31")], foreground=[("active", "#ffffff")])
        style.configure("NavActive.TButton", font=("Segoe UI", 11, "bold"), padding=(18, 13),
                        background="#1c1a39", foreground="#ffffff", bordercolor=palette["accent"], anchor="w")
        style.configure("Tab.TButton", font=("Segoe UI", 10), padding=(12, 7),
                        background=palette["panel"], foreground=palette["muted"], borderwidth=0)
        style.configure("TabActive.TButton", font=("Segoe UI", 10, "bold"), padding=(12, 7),
                        background="#24184a", foreground="#b99aff", bordercolor=palette["accent"])
        style.configure("Otp.Treeview", background="#0c1222", fieldbackground="#0c1222",
                        foreground="#e8eaf2", rowheight=43, bordercolor=palette["border"],
                        font=("Segoe UI", 10))
        style.configure("Otp.Treeview.Heading", background="#191c39", foreground="#bbc3dc",
                        font=("Segoe UI", 9, "bold"), relief="flat", padding=(6, 8))
        style.map("Otp.Treeview", background=[("selected", "#1c2340")],
                  foreground=[("selected", "#ffffff")])
        style.map("Otp.Treeview.Heading", background=[("active", "#232746"), ("pressed", "#232746")],
                  foreground=[("active", "#ffffff")])
        style.configure("Report.Treeview", background="#0c1222", fieldbackground="#0c1222",
                        foreground="#e8eaf2", rowheight=38, bordercolor=palette["border"],
                        font=("Segoe UI", 10), relief="flat")
        style.configure("Report.Treeview.Heading", background="#191c39", foreground="#bbc3dc",
                        font=("Segoe UI", 9, "bold"), relief="flat", padding=(8, 9))
        style.map("Report.Treeview", background=[("selected", "#252c4b")],
                  foreground=[("selected", "#ffffff")])
        style.map("Report.Treeview.Heading", background=[("active", "#232746"), ("pressed", "#232746")],
                  foreground=[("active", "#ffffff")])
        style.configure("Dark.Vertical.TScrollbar", background="#252c4b", troughcolor="#0c1222",
                        bordercolor="#29324d", arrowcolor="#bbc3dc", relief="flat", width=14)
        style.map("Dark.Vertical.TScrollbar", background=[("active", "#384260"), ("pressed", "#4b5678")])

        self.key_var = tk.StringVar(value=str(self.saved_settings.get("api_key", "")))
        self.price_var = tk.StringVar(value="—")
        self.balance_var = tk.StringVar(value="—")
        self.stock_var = tk.StringVar(value="สินค้า —")
        self.table_filter = "active"
        self.search_var = tk.StringVar()

        shell = tk.Frame(self, bg=palette["window"])
        shell.pack(fill="both", expand=True)
        shell.grid_rowconfigure(0, weight=1); shell.grid_columnconfigure(1, weight=1)

        sidebar = tk.Frame(shell, bg=palette["sidebar"], width=205,
                           highlightthickness=1, highlightbackground="#1f2940")
        sidebar.grid(row=0, column=0, sticky="nsw"); sidebar.grid_propagate(False)
        brand = tk.Frame(sidebar, bg=palette["sidebar"], padx=20, pady=25)
        brand.pack(fill="x")
        logo = tk.Label(brand, text="24\nHR", bg="#33206f", fg="#b89cff",
                        font=("Segoe UI", 11, "bold"), width=4, height=2)
        logo.pack(side="left")
        brand_text = tk.Frame(brand, bg=palette["sidebar"]); brand_text.pack(side="left", padx=(11, 0))
        tk.Label(brand_text, text="OTP24HR", bg=palette["sidebar"], fg=palette["text"],
                 font=("Segoe UI", 16, "bold")).pack(anchor="w")
        tk.Label(brand_text, text="by STWIN", bg=palette["sidebar"], fg=palette["muted"],
                 font=("Segoe UI", 9)).pack(anchor="w")

        nav = tk.Frame(sidebar, bg=palette["sidebar"], padx=14, pady=12); nav.pack(fill="x")
        self.home_nav_btn = ttk.Button(nav, text="⌂   หน้าหลัก", style="NavActive.TButton",
                                       command=lambda: self._set_table_filter("active"))
        self.home_nav_btn.pack(fill="x", pady=(0, 5))
        self.history_nav_btn = ttk.Button(nav, text="↶   ประวัติ OTP", style="Nav.TButton",
                                          command=lambda: self._set_table_filter("success"))
        self.history_nav_btn.pack(fill="x", pady=5)
        self.admin_report_btn = ttk.Button(nav, text="▥   รายงาน", style="Nav.TButton",
                                           command=self._show_admin_report)
        self.create_user_btn = ttk.Button(nav, text="♙   จัดการสมาชิก", style="Nav.TButton",
                                          command=self._show_create_user)

        nav_bottom = tk.Frame(sidebar, bg=palette["sidebar"], padx=14, pady=18)
        nav_bottom.pack(fill="x", side="bottom")
        self.settings_btn = ttk.Button(nav_bottom, text="⚙   ตั้งค่า", style="Nav.TButton",
                                       command=self._show_settings)
        self.settings_btn.pack(fill="x", pady=4)
        self.update_btn = ttk.Button(nav_bottom, text="↥   ตรวจสอบอัปเดต", style="Nav.TButton",
                                     command=lambda: self._check_for_updates(False))
        self.update_btn.pack(fill="x", pady=4)
        self.logout_btn = ttk.Button(nav_bottom, text="↪   ออกจากระบบ", style="Nav.TButton",
                                     command=self._logout)
        self.logout_btn.pack(fill="x", pady=4)

        main = tk.Frame(shell, bg=palette["window"], padx=28, pady=20)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1); main.grid_rowconfigure(4, weight=1)

        header = tk.Frame(main, bg=palette["window"], height=56)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14)); header.grid_propagate(False)
        tk.Label(header, text="รายการ OTP", bg=palette["window"], fg=palette["text"],
                 font=("Segoe UI", 22, "bold")).pack(side="left", anchor="s")
        tk.Label(header, text=f"v{APP_VERSION}", bg="#171d31", fg="#aeb7d0",
                 font=("Segoe UI", 9), padx=9, pady=5).pack(side="right", pady=12)
        self.user_badge = tk.Label(header, text="ผู้ใช้: —", bg=palette["window"], fg=palette["text"],
                                   font=("Segoe UI", 10, "bold"), padx=17)
        self.user_badge.pack(side="right")
        tk.Label(header, text="●  ออนไลน์", bg=palette["window"], fg=palette["success"],
                 font=("Segoe UI", 9)).pack(side="right")

        balance_strip = tk.Frame(main, bg=palette["panel"], padx=20, pady=15,
                                 highlightthickness=1, highlightbackground=palette["border"])
        balance_strip.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        balance_strip.grid_columnconfigure(0, weight=1); balance_strip.grid_columnconfigure(1, weight=1)
        balance_box = tk.Frame(balance_strip, bg=palette["panel"]); balance_box.grid(row=0, column=0, sticky="w")
        tk.Label(balance_box, text="▣   ยอดเงินคงเหลือ", bg=palette["panel"], fg=palette["muted"],
                 font=("Segoe UI", 9)).pack(anchor="w")
        tk.Label(balance_box, textvariable=self.balance_var, bg=palette["panel"], fg=palette["text"],
                 font=("Segoe UI", 16, "bold")).pack(anchor="w", pady=(2, 0))
        price_box = tk.Frame(balance_strip, bg=palette["panel"]); price_box.grid(row=0, column=1, sticky="w")
        tk.Label(price_box, text="◇   ราคา / เบอร์", bg=palette["panel"], fg=palette["muted"],
                 font=("Segoe UI", 9)).pack(anchor="w")
        tk.Label(price_box, textvariable=self.price_var, bg=palette["panel"], fg=palette["text"],
                 font=("Segoe UI", 16, "bold")).pack(anchor="w", pady=(2, 0))
        self.topup_btn = ttk.Button(balance_strip, text="เติมเงิน  ＋", command=self._topup)

        purchase = tk.Frame(main, bg=palette["panel"], padx=20, pady=14,
                            highlightthickness=1, highlightbackground=palette["border"])
        purchase.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        tk.Label(purchase, text="ซื้อหมายเลข", bg=palette["panel"], fg=palette["text"],
                 font=("Segoe UI", 14, "bold")).pack(side="left")
        tk.Label(purchase, text="จำนวน", bg=palette["panel"], fg=palette["muted"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(45, 8))
        self.qty_var = tk.IntVar(value=1)
        ttk.Spinbox(purchase, from_=1, to=5, textvariable=self.qty_var, width=4, justify="center",
                    state="readonly", font=("Segoe UI", 10)).pack(side="left")
        self.buy_btn = ttk.Button(purchase, text="ซื้อหมายเลข", command=self.buy, state="disabled", style="Green.TButton")
        self.buy_btn.pack(side="left", padx=(18, 0))
        tk.Label(purchase, textvariable=self.stock_var, bg=palette["panel"], fg=palette["muted"],
                 font=("Segoe UI", 9)).pack(side="right")
        self.refresh_btn = ttk.Button(purchase, text="รีเฟรชราคา", command=self.refresh)
        self.refresh_btn.pack(side="right", padx=(0, 16))

        self.notice_var = tk.StringVar(value="กรุณาเข้าสู่ระบบ")
        self.notice = tk.Label(main, textvariable=self.notice_var, bg=palette["window"], fg=palette["muted"],
                               font=("Segoe UI", 9), anchor="w")
        self.notice.grid(row=3, column=0, sticky="ew", pady=(0, 8))

        table_card = tk.Frame(main, bg=palette["panel"], padx=18, pady=16,
                              highlightthickness=1, highlightbackground=palette["border"])
        table_card.grid(row=4, column=0, sticky="nsew")
        table_card.grid_columnconfigure(0, weight=1); table_card.grid_rowconfigure(1, weight=1)
        tools = tk.Frame(table_card, bg=palette["panel"]); tools.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.tab_buttons = {}
        for key, label in (("active", "กำลังใช้งาน"), ("success", "สำเร็จ"), ("all", "ทั้งหมด")):
            button = ttk.Button(tools, text=label, style="Tab.TButton",
                                command=lambda value=key: self._set_table_filter(value))
            button.pack(side="left", padx=(0, 7)); self.tab_buttons[key] = button
        self.tab_buttons["active"].configure(style="TabActive.TButton")
        search = ttk.Entry(tools, textvariable=self.search_var, width=27, font=("Segoe UI", 10))
        search.pack(side="right"); search.insert(0, "")
        tk.Label(tools, text="ค้นหา", bg=palette["panel"], fg=palette["muted"],
                 font=("Segoe UI", 9)).pack(side="right", padx=(0, 8))
        self.search_var.trace_add("write", self._on_search_changed)

        table_frame = tk.Frame(table_card, bg=palette["panel"])
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.grid_rowconfigure(0, weight=1); table_frame.grid_columnconfigure(0, weight=1)
        self.table = ttk.Treeview(table_frame, columns=("number", "time", "status", "code", "actions"),
                                  show="headings", selectmode="browse", style="Otp.Treeview")
        table_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview,
                                     style="Dark.Vertical.TScrollbar")
        self.table.configure(yscrollcommand=table_scroll.set)
        for col, title, width, stretch in (("number", "หมายเลข", 240, True),
                                           ("time", "เวลา", 125, False), ("status", "สถานะ", 240, True),
                                           ("code", "OTP", 120, False), ("actions", "การทำงาน", 90, False)):
            self.table.heading(col, text=title)
            self.table.column(col, width=width, minwidth=70, anchor="center", stretch=stretch)
        self.table.grid(row=0, column=0, sticky="nsew"); table_scroll.grid(row=0, column=1, sticky="ns")
        self.table.bind("<Button-3>", self._show_order_menu)
        self.table.bind("<<TreeviewSelect>>", lambda _e: self._update_action_bar())
        self.table.bind("<Double-1>", self._copy_otp_or_number)

        self.action_bar = tk.Frame(table_card, bg="#0c1222", padx=12, pady=9,
                                   highlightthickness=1, highlightbackground="#343d5c")
        self.action_bar.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        self.selection_var = tk.StringVar(value="เลือกรายการเพื่อจัดการ")
        tk.Label(self.action_bar, textvariable=self.selection_var, bg="#0c1222", fg=palette["muted"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(4, 18))
        self.cancel_action_btn = ttk.Button(self.action_bar, text="ยกเลิก", style="Danger.TButton",
                                             command=lambda: self.command_selected("cancel"))
        self.cancel_action_btn.pack(side="right")
        self.complete_action_btn = ttk.Button(self.action_bar, text="เสร็จสิ้น",
                                               command=lambda: self.command_selected("complete"))
        self.complete_action_btn.pack(side="right", padx=7)
        self.resend_action_btn = ttk.Button(self.action_bar, text="ขอ OTP ซ้ำ",
                                             command=lambda: self.command_selected("resend"))
        self.resend_action_btn.pack(side="right", padx=(0, 7))
        self.poll_action_btn = ttk.Button(self.action_bar, text="ตรวจ OTP ตอนนี้", command=self.poll_selected)
        self.poll_action_btn.pack(side="right", padx=(0, 7))
        self._update_action_bar()

    def _build_ui(self):
        ctk.set_appearance_mode("dark")
        colors = {
            "window": "#070b16", "sidebar": "#090e1c", "panel": "#10162a",
            "panel_2": "#141a31", "border": "#29324d", "accent": "#7c3aed",
            "accent_hover": "#8b5cf6", "text": "#f7f4ff", "muted": "#9da7c3",
            "success": "#38d39f", "danger": "#ef4653", "warning": "#f59e0b",
        }
        self.palette = colors
        self.configure(bg=colors["window"])

        style = ttk.Style(self); style.theme_use("clam")
        style.configure("TEntry", fieldbackground="#0c1222", foreground=colors["text"],
                        insertcolor="#ffffff", padding=9, bordercolor=colors["border"])
        style.configure("TButton", font=("Leelawadee UI", 10, "bold"), padding=(14, 10),
                        background="#171e35", foreground=colors["text"], bordercolor="#35405f")
        style.map("TButton", background=[("active", "#222b48"), ("disabled", "#101526")],
                  foreground=[("disabled", "#59627a")])
        style.configure("Green.TButton", background=colors["accent"], foreground="#ffffff",
                        bordercolor="#9b6cff", padding=(20, 11))
        style.configure("Report.Treeview", background="#0c1222", fieldbackground="#0c1222",
                        foreground="#e8eaf2", rowheight=38, bordercolor=colors["border"], font=("Segoe UI", 10))
        style.configure("Report.Treeview.Heading", background="#191c39", foreground="#bbc3dc",
                        font=("Leelawadee UI", 9, "bold"), relief="flat", padding=(8, 9))
        style.map("Report.Treeview", background=[("selected", "#252c4b")], foreground=[("selected", "#ffffff")])
        style.map("Report.Treeview.Heading", background=[("active", "#232746")], foreground=[("active", "#ffffff")])
        style.configure("Dark.Vertical.TScrollbar", background="#252c4b", troughcolor="#0c1222",
                        bordercolor="#29324d", arrowcolor="#bbc3dc", relief="flat", width=14)

        self.key_var = tk.StringVar(value=str(self.saved_settings.get("api_key", "")))
        self.price_var = tk.StringVar(value="—")
        self.balance_var = tk.StringVar(value="—")
        self.stock_var = tk.StringVar(value="คงเหลือ —")
        self.table_filter = "active"
        self.search_var = tk.StringVar()
        self.qty_var = tk.IntVar(value=1)
        self._last_tab_counts = None

        shell = ctk.CTkFrame(self, fg_color=colors["window"], corner_radius=0)
        shell.pack(fill="both", expand=True)
        shell.grid_rowconfigure(0, weight=1); shell.grid_columnconfigure(1, weight=1)

        sidebar = ctk.CTkFrame(shell, width=198, fg_color=colors["sidebar"], corner_radius=0,
                               border_width=1, border_color="#202943")
        sidebar.grid(row=0, column=0, sticky="nsw"); sidebar.grid_propagate(False)
        brand = ctk.CTkFrame(sidebar, fg_color="transparent", height=102)
        brand.pack(fill="x", padx=22, pady=(18, 8)); brand.pack_propagate(False)
        ctk.CTkLabel(brand, text="24\nHR", width=44, height=44, corner_radius=7,
                     fg_color="#34206f", text_color="#a98cff",
                     font=ctk.CTkFont("Segoe UI", 13, "bold")).pack(side="left")
        brand_words = ctk.CTkFrame(brand, fg_color="transparent"); brand_words.pack(side="left", padx=(12, 0))
        ctk.CTkLabel(brand_words, text="OTP24HR", text_color=colors["text"],
                     font=ctk.CTkFont("Segoe UI", 20, "bold")).pack(anchor="w")
        ctk.CTkLabel(brand_words, text="by STWIN", text_color=colors["muted"],
                     font=ctk.CTkFont("Segoe UI", 11)).pack(anchor="w")

        nav = ctk.CTkFrame(sidebar, fg_color="transparent"); nav.pack(fill="x", padx=15, pady=(4, 0))
        nav_font = ctk.CTkFont("Leelawadee UI", 15)
        active_nav = {"height": 48, "corner_radius": 7, "anchor": "w", "font": nav_font,
                      "fg_color": "#1a1835", "hover_color": "#252047", "text_color": "#ffffff",
                      "border_width": 1, "border_color": colors["accent"]}
        plain_nav = {"height": 48, "corner_radius": 7, "anchor": "w", "font": nav_font,
                     "fg_color": "transparent", "hover_color": "#151b31", "text_color": "#aeb7cf",
                     "border_width": 0}
        self.home_nav_btn = ctk.CTkButton(nav, text="⌂    หน้าหลัก", command=lambda: self._set_table_filter("active"), **active_nav)
        self.home_nav_btn.pack(fill="x", pady=4)
        self.history_nav_btn = ctk.CTkButton(nav, text="◷    ประวัติ OTP", command=lambda: self._set_table_filter("success"), **plain_nav)
        self.history_nav_btn.pack(fill="x", pady=4)
        self.admin_report_btn = ctk.CTkButton(nav, text="▥    รายงาน", command=self._show_admin_report, **plain_nav)
        self.create_user_btn = ctk.CTkButton(nav, text="♙    จัดการสมาชิก", command=self._show_create_user, **plain_nav)

        nav_bottom = ctk.CTkFrame(sidebar, fg_color="transparent"); nav_bottom.pack(fill="x", side="bottom", padx=15, pady=19)
        self.settings_btn = ctk.CTkButton(nav_bottom, text="⚙    ตั้งค่า", command=self._show_settings, **plain_nav)
        self.settings_btn.pack(fill="x", pady=3)
        self.update_btn = ctk.CTkButton(nav_bottom, text="↥    ตรวจสอบอัปเดต",
                                        command=lambda: self._check_for_updates(False), **plain_nav)
        self.update_btn.pack(fill="x", pady=3)
        self.logout_btn = ctk.CTkButton(nav_bottom, text="↪    ออกจากระบบ", command=self._logout, **plain_nav)
        self.logout_btn.pack(fill="x", pady=3)

        main = ctk.CTkFrame(shell, fg_color=colors["window"], corner_radius=0)
        main.grid(row=0, column=1, sticky="nsew", padx=28, pady=17)
        main.grid_columnconfigure(0, weight=1); main.grid_rowconfigure(3, weight=1)

        header = ctk.CTkFrame(main, height=68, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12)); header.grid_propagate(False)
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="รายการ OTP", text_color=colors["text"], anchor="w",
                     font=ctk.CTkFont("Leelawadee UI", 25, "bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(header, text="●  ออนไลน์", text_color=colors["success"],
                     font=ctk.CTkFont("Leelawadee UI", 13)).grid(row=0, column=1, padx=(0, 20))
        ctk.CTkFrame(header, width=1, height=35, fg_color="#303850").grid(row=0, column=2, padx=(0, 20))
        profile = tk.Canvas(header, width=42, height=42, bg=colors["window"], highlightthickness=0)
        profile.grid(row=0, column=3)
        profile.create_oval(1, 1, 41, 41, fill="#312651", outline="#544477", width=1)
        profile.create_oval(15, 9, 27, 21, outline="#f1edff", width=2)
        profile.create_arc(10, 19, 32, 36, start=0, extent=180, style="arc", outline="#f1edff", width=2)
        self.user_badge = ctk.CTkLabel(header, text="—", text_color=colors["text"],
                                       font=ctk.CTkFont("Segoe UI", 14, "bold"))
        self.user_badge.grid(row=0, column=4, padx=(10, 16))
        ctk.CTkFrame(header, width=1, height=35, fg_color="#303850").grid(row=0, column=5, padx=(0, 16))
        ctk.CTkLabel(header, text=f"v{APP_VERSION}", width=58, height=28, corner_radius=5,
                     fg_color="#171d31", text_color="#b6bfd7", font=ctk.CTkFont("Segoe UI", 10)).grid(row=0, column=6)

        balance = ctk.CTkFrame(main, height=85, fg_color=colors["panel"], corner_radius=9,
                               border_width=1, border_color=colors["border"])
        balance.grid(row=1, column=0, sticky="ew", pady=(0, 11)); balance.grid_propagate(False)
        balance.grid_columnconfigure(0, weight=1); balance.grid_columnconfigure(2, weight=1)
        wallet = ctk.CTkFrame(balance, fg_color="transparent"); wallet.grid(row=0, column=0, sticky="w", padx=21, pady=15)
        ctk.CTkLabel(wallet, text="▣   ยอดเงินคงเหลือ", text_color=colors["muted"],
                     font=ctk.CTkFont("Leelawadee UI", 11)).pack(anchor="w")
        ctk.CTkLabel(wallet, textvariable=self.balance_var, text_color=colors["text"],
                     font=ctk.CTkFont("Segoe UI", 20, "bold")).pack(anchor="w", pady=(1, 0))
        ctk.CTkFrame(balance, width=1, height=45, fg_color="#303850").grid(row=0, column=1)
        price = ctk.CTkFrame(balance, fg_color="transparent"); price.grid(row=0, column=2, sticky="w", padx=28, pady=15)
        ctk.CTkLabel(price, text="◇   ราคา/เบอร์", text_color=colors["muted"],
                     font=ctk.CTkFont("Leelawadee UI", 11)).pack(anchor="w")
        price_line = ctk.CTkFrame(price, fg_color="transparent"); price_line.pack(anchor="w", pady=(1, 0))
        ctk.CTkLabel(price_line, textvariable=self.price_var, text_color="#c4b5fd",
                     font=ctk.CTkFont("Segoe UI", 21, "bold")).pack(side="left")
        ctk.CTkLabel(price_line, textvariable=self.stock_var, text_color="#77819d",
                     font=ctk.CTkFont("Leelawadee UI", 10)).pack(side="left", padx=(12, 0), pady=(5, 0))
        self.refresh_btn = ctk.CTkButton(balance, text="↻  อัปเดตราคา", width=118, height=40,
                                         fg_color="#21183f", hover_color="#302257", text_color="#c8b5ff",
                                         border_width=1, border_color="#6545a7", corner_radius=7,
                                         font=ctk.CTkFont("Leelawadee UI", 12, "bold"), command=self.refresh)
        self.refresh_btn.grid(row=0, column=3, padx=(8, 8), sticky="e")
        self.topup_btn = ctk.CTkButton(balance, text="เติมเงิน   ＋", width=112, height=40,
                                       fg_color="transparent", hover_color="#251b48", text_color="#bca4ff",
                                       border_width=1, border_color=colors["accent"], corner_radius=7,
                                       font=ctk.CTkFont("Leelawadee UI", 13, "bold"), command=self._topup)

        purchase = ctk.CTkFrame(main, height=87, fg_color=colors["panel"], corner_radius=9,
                                border_width=1, border_color=colors["border"])
        purchase.grid(row=2, column=0, sticky="ew", pady=(0, 14)); purchase.grid_propagate(False)
        purchase.grid_columnconfigure(4, weight=1)
        ctk.CTkLabel(purchase, text="ซื้อหมายเลข", text_color=colors["text"],
                     font=ctk.CTkFont("Leelawadee UI", 18, "bold")).grid(row=0, column=0, padx=(20, 45))
        ctk.CTkLabel(purchase, text="จำนวน", text_color=colors["muted"],
                     font=ctk.CTkFont("Leelawadee UI", 11)).grid(row=0, column=1, padx=(0, 10))
        stepper = ctk.CTkFrame(purchase, width=128, height=43, fg_color="#0c1222", corner_radius=7,
                               border_width=1, border_color="#29324d")
        stepper.grid(row=0, column=2); stepper.grid_propagate(False)
        ctk.CTkButton(stepper, text="−", width=41, height=41, fg_color="#171e34", hover_color="#252e49",
                      corner_radius=6, font=ctk.CTkFont("Segoe UI", 20),
                      command=lambda: self.qty_var.set(max(1, self.qty_var.get() - 1))).pack(side="left")
        ctk.CTkLabel(stepper, textvariable=self.qty_var, width=45, text_color=colors["text"],
                     font=ctk.CTkFont("Segoe UI", 16, "bold")).pack(side="left", fill="y")
        ctk.CTkButton(stepper, text="+", width=41, height=41, fg_color="#171e34", hover_color="#252e49",
                      corner_radius=6, font=ctk.CTkFont("Segoe UI", 19),
                      command=lambda: self.qty_var.set(min(5, self.qty_var.get() + 1))).pack(side="right")
        ctk.CTkLabel(purchase, text="(1–5)", text_color=colors["muted"],
                     font=ctk.CTkFont("Segoe UI", 12)).grid(row=0, column=3, padx=12)
        self.buy_btn = ctk.CTkButton(purchase, text="🛒   ซื้อหมายเลข", width=190, height=48,
                                     fg_color=colors["accent"], hover_color=colors["accent_hover"],
                                     text_color="#ffffff", corner_radius=7,
                                     font=ctk.CTkFont("Leelawadee UI", 15, "bold"),
                                     command=self.buy, state="disabled")
        self.buy_btn.grid(row=0, column=4, padx=(10, 35))
        status_area = ctk.CTkFrame(purchase, fg_color="transparent"); status_area.grid(row=0, column=5, padx=(0, 20))
        self.daily_otp_var = tk.StringVar(value="ยังไม่มีประวัติ OTP สำเร็จ")
        self.daily_otp = tk.Label(status_area, textvariable=self.daily_otp_var, bg=colors["panel"], fg="#bca4ff",
                                  font=("Leelawadee UI", 9, "bold"), anchor="e", justify="right", wraplength=250)
        self.daily_otp.pack(anchor="e", pady=(0, 2))
        self.notice_var = tk.StringVar(value="กรุณาเข้าสู่ระบบ")
        self.notice = tk.Label(status_area, textvariable=self.notice_var, bg=colors["panel"], fg="#8f9ab7",
                               font=("Leelawadee UI", 9), anchor="e", justify="right", wraplength=250)
        self.notice.pack(anchor="e")

        table_card = ctk.CTkFrame(main, fg_color=colors["panel"], corner_radius=9,
                                  border_width=1, border_color=colors["border"])
        table_card.grid(row=3, column=0, sticky="nsew")
        table_card.grid_columnconfigure(0, weight=1); table_card.grid_rowconfigure(1, weight=1)
        tools = ctk.CTkFrame(table_card, height=68, fg_color="transparent")
        tools.grid(row=0, column=0, sticky="ew", padx=20, pady=(4, 0)); tools.grid_propagate(False)
        tools.grid_columnconfigure(3, weight=1)
        self.tab_buttons = {}
        self.tab_underlines = {}
        for index, (key, label) in enumerate((("active", "กำลังใช้งาน"), ("success", "สำเร็จ"), ("all", "ทั้งหมด"))):
            active = key == "active"
            button = ctk.CTkButton(tools, text=label, width=112, height=42, corner_radius=6,
                                   fg_color="transparent",
                                   hover_color="#28203f", text_color="#b99aff" if active else colors["muted"],
                                   border_width=0, border_color=colors["accent"],
                                   font=ctk.CTkFont("Leelawadee UI", 13, "bold" if active else "normal"),
                                   command=lambda value=key: self._set_table_filter(value))
            button.grid(row=0, column=index, padx=(0, 7)); self.tab_buttons[key] = button
            underline = ctk.CTkFrame(tools, height=2, fg_color=colors["accent"] if active else "transparent",
                                     corner_radius=0)
            underline.grid(row=1, column=index, sticky="ew", padx=(4, 11)); self.tab_underlines[key] = underline
        search = ctk.CTkEntry(tools, textvariable=self.search_var, width=260, height=42,
                              placeholder_text="ค้นหาเบอร์, OTP...", fg_color="#0b1120",
                              border_color="#35405d", text_color=colors["text"],
                              placeholder_text_color="#737d99", corner_radius=7,
                              font=ctk.CTkFont("Leelawadee UI", 12))
        search.grid(row=0, column=4, padx=(10, 10))
        ctk.CTkButton(tools, text="▽  ตัวกรอง", width=110, height=42, fg_color="transparent",
                      hover_color="#202941", text_color="#b7bfd5", border_width=1,
                      border_color="#35405d", corner_radius=7,
                      font=ctk.CTkFont("Leelawadee UI", 12),
                      command=lambda: self._set_table_filter("all")).grid(row=0, column=5)
        self.search_var.trace_add("write", self._on_search_changed)

        self.table = OrderListView(table_card, on_action=self._table_action,
                                   on_more=self._show_order_menu_at,
                                   on_copy_number=self._copy_order_number)
        self.table.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 7))
        footer = ctk.CTkFrame(table_card, height=42, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=22, pady=(0, 8)); footer.grid_propagate(False)
        footer.grid_columnconfigure(1, weight=1)
        self.list_summary_var = tk.StringVar(value="แสดง 0 รายการ")
        ctk.CTkLabel(footer, textvariable=self.list_summary_var, text_color=colors["muted"],
                     font=ctk.CTkFont("Leelawadee UI", 10)).grid(row=0, column=0, sticky="w")
        self.page_status_var = tk.StringVar(value="หน้า 1 จาก 1")
        ctk.CTkLabel(footer, textvariable=self.page_status_var, text_color=colors["muted"],
                     font=ctk.CTkFont("Leelawadee UI", 10)).grid(row=0, column=1)
        self.prev_page_btn = ctk.CTkButton(
            footer, text="‹", width=34, height=31, fg_color="#171e34", hover_color="#282342",
            border_width=1, border_color="#35405d", corner_radius=6,
            font=ctk.CTkFont("Segoe UI", 17), command=lambda: self._change_history_page(-1), state="disabled"
        )
        self.prev_page_btn.grid(row=0, column=2, padx=(0, 6))
        self.current_page_btn = ctk.CTkButton(
            footer, text="1", width=34, height=31, fg_color="#271a50", hover_color="#382568",
            border_width=1, border_color=colors["accent"], corner_radius=6,
            font=ctk.CTkFont("Segoe UI", 11), state="disabled"
        )
        self.current_page_btn.grid(row=0, column=3)
        self.next_page_btn = ctk.CTkButton(
            footer, text="›", width=34, height=31, fg_color="#171e34", hover_color="#282342",
            border_width=1, border_color="#35405d", corner_radius=6,
            font=ctk.CTkFont("Segoe UI", 17), command=lambda: self._change_history_page(1), state="disabled"
        )
        self.next_page_btn.grid(row=0, column=4, padx=(6, 0))

    def _table_action(self, name):
        if name == "poll":
            self.poll_selected()
        else:
            self.command_selected(name)

    def _show_order_menu_at(self, iid, x_root, y_root):
        self.table.selection_set(iid)
        event = type("PopupEvent", (), {"x_root": x_root, "y_root": y_root, "y": 0})()
        self._show_order_menu(event, row_override=iid)

    def _copy_order_number(self, iid):
        order = self.orders.get(iid)
        if not order:
            return
        phone = order.get("phone", "")
        if phone:
            self.clipboard_clear()
            self.clipboard_append(phone)
            self.notice_var.set(f"คัดลอกหมายเลข {phone} แล้ว")

    def _set_table_filter(self, value):
        self.table_filter = value
        home_active = value != "success"
        self.home_nav_btn.configure(fg_color="#1a1835" if home_active else "transparent",
                                    text_color="#ffffff" if home_active else "#aeb7cf",
                                    border_width=1 if home_active else 0)
        self.history_nav_btn.configure(fg_color="#1a1835" if not home_active else "transparent",
                                       text_color="#ffffff" if not home_active else "#aeb7cf",
                                       border_width=1 if not home_active else 0,
                                       border_color=self.palette["accent"])
        for key, button in self.tab_buttons.items():
            active = key == value
            button.configure(fg_color="transparent",
                             text_color="#b99aff" if active else self.palette["muted"],
                             border_width=0,
                             border_color=self.palette["accent"])
            self.tab_underlines[key].configure(fg_color=self.palette["accent"] if active else "transparent")
        if value in ("success", "all") and self.cloud_user:
            self._load_history_page(reset=True)
        else:
            self._render_orders()

    def _on_search_changed(self, *_):
        if self.table_filter == "active" or not self.cloud_user:
            self._render_orders()
            return
        if self.history_search_job:
            try:
                self.after_cancel(self.history_search_job)
            except tk.TclError:
                pass
        self.history_search_job = self.after(300, lambda: self._load_history_page(reset=True))

    def _change_history_page(self, direction):
        if self.table_filter not in ("success", "all") or self.history_loading:
            return
        pages = max(1, (self.history_total + self.history_page_size - 1) // self.history_page_size)
        target = min(pages, max(1, self.history_page + direction))
        if target != self.history_page:
            self.history_page = target
            self._load_history_page()

    def _order_matches_filter(self, order):
        completed = self._order_is_success(order)
        # Keep received OTPs in the working list until the user explicitly
        # presses "เสร็จสิ้น".  Receiving an OTP stops polling, but must not
        # move the row away while the user is still copying/using the code.
        if self.table_filter == "active" and not (order.get("active") or (completed and order.get("in_working", False))):
            return False
        if self.table_filter == "success" and not completed:
            return False
        query = self.search_var.get().strip().lower()
        return not query or query in " ".join(str(order.get(key, "")) for key in ("phone", "status", "code", "buyer")).lower()

    @staticmethod
    def _order_is_success(order):
        return (order.get("outcome") == "success" or order.get("cloud_recorded") is True
                or order.get("code") not in (None, "", "—")
                or "ได้รับ OTP" in str(order.get("status", "")))

    def _render_orders(self):
        if not hasattr(self, "table"):
            return
        selected = self._selected_id()
        if hasattr(self.table, "clear"):
            self.table.clear()
        else:
            for item in self.table.get_children():
                self.table.delete(item)
        source = self.orders.items()
        if self.table_filter in ("success", "all") and self.cloud_user:
            source = ((aid, self.orders[aid]) for aid in self.history_page_ids if aid in self.orders)
        for aid, order in source:
            if self._order_matches_filter(order):
                self.table.insert("", "end", iid=aid)
                self._sync_row(aid)
        if selected and self.table.exists(selected):
            self.table.selection_set(selected)
        self._update_tab_labels()
        self._update_action_bar()
        self._update_pagination()

    def _update_pagination(self):
        if not hasattr(self, "list_summary_var"):
            return
        visible = len(self.table.get_children())
        paged = self.table_filter in ("success", "all") and bool(self.cloud_user)
        total = self.history_total if paged else visible
        pages = max(1, (total + self.history_page_size - 1) // self.history_page_size) if paged else 1
        page = min(max(1, self.history_page), pages) if paged else 1
        start = (page - 1) * self.history_page_size + 1 if visible else 0
        end = start + visible - 1 if visible else 0
        self.list_summary_var.set(f"แสดง {start}–{end} จาก {total} รายการ" if visible else "ไม่มีรายการ")
        self.page_status_var.set(f"หน้า {page} จาก {pages}")
        self.current_page_btn.configure(text=str(page))
        self.prev_page_btn.configure(state="normal" if paged and page > 1 and not self.history_loading else "disabled")
        self.next_page_btn.configure(state="normal" if paged and page < pages and not self.history_loading else "disabled")

    def _update_tab_labels(self):
        if not hasattr(self, "tab_buttons"):
            return
        active = sum(1 for order in self.orders.values()
                     if order.get("active") or (self._order_is_success(order) and order.get("in_working", False)))
        success = self.history_counts.get("success", 0) if self.cloud_user else sum(
            1 for order in self.orders.values() if self._order_is_success(order))
        total = self.history_counts.get("all", 0) if self.cloud_user else len(self.orders)
        counts = (active, success, total)
        if getattr(self, "_last_tab_counts", None) == counts:
            return
        self._last_tab_counts = counts
        for key, text, count in (("active", "กำลังใช้งาน", active), ("success", "สำเร็จ", success),
                                 ("all", "ทั้งหมด", total)):
            self.tab_buttons[key].configure(text=f"{text}  {count}")

    def _update_action_bar(self):
        selected = self._selected_id() if hasattr(self, "table") else None
        state = "normal" if selected else "disabled"
        for button in (getattr(self, "poll_action_btn", None), getattr(self, "resend_action_btn", None),
                       getattr(self, "complete_action_btn", None), getattr(self, "cancel_action_btn", None)):
            if button:
                button.configure(state=state)
        if hasattr(self, "selection_var"):
            phone = self.orders.get(selected, {}).get("phone") if selected else None
            self.selection_var.set(f"เลือกแล้ว: {phone}" if phone else "เลือกรายการเพื่อจัดการ")

    def _copy_otp_or_number(self, _event=None):
        aid = self._selected_id()
        if not aid:
            return
        order = self.orders[aid]
        value = order.get("code") if order.get("code") not in (None, "", "—") else order.get("phone", "")
        if value:
            self.clipboard_clear(); self.clipboard_append(value)
            self.notice_var.set(f"คัดลอก {value} แล้ว")

    def _apply_dark_palette(self, root):
        """Convert the plain Tk parts of the web layout to the original dark-blue palette."""
        backgrounds = {
            "#ffffff": "#100b20", "#f3f8f4": "#19102f", "#061321": "#070510",
            "#0b1f36": "#100b20", "#102a46": "#19102f", "#294866": "#5b21b6",
        }
        foregrounds = {
            "#13231a": "#f5f3ff", "#617067": "#b9a7d4", "#6d7c72": "#b9a7d4",
            "#53675a": "#d8c8ef", "#3b4b40": "#d8c8ef", "#08783a": "#c084fc",
            "#58d6ff": "#c084fc",
        }
        for widget in root.winfo_children():
            try:
                bg = widget.cget("background")
                if bg in backgrounds: widget.configure(background=backgrounds[bg])
            except (tk.TclError, AttributeError, ValueError): pass
            try:
                fg = widget.cget("foreground")
                if fg in foregrounds: widget.configure(foreground=foregrounds[fg])
            except (tk.TclError, AttributeError, ValueError): pass
            if widget.winfo_children(): self._apply_dark_palette(widget)

    def _require_login(self):
        while not self.cloud_user and self.winfo_exists():
            credentials = self._show_login_dialog()
            if credentials is None:
                self.destroy(); return
            username, password, remember = credentials
            try:
                result = self.cloud.login(username.strip(), password)
                if remember:
                    self.saved_settings.update(username=username.strip(), password=password, api_key=self.key_var.get().strip())
                    self.credential_store.save(self.saved_settings)
                else:
                    self.saved_settings.pop("username", None); self.saved_settings.pop("password", None)
                    self.credential_store.save(self.saved_settings)
                self._login_complete(result)
            except Exception as exc:
                messagebox.showerror("เข้าสู่ระบบไม่สำเร็จ", str(exc))

    def _login_complete(self, result):
        self.cloud_user = result["username"]
        self.cloud_role = result.get("role", "user")
        self._switch_user_orders(self.cloud_user)
        self.user_badge.configure(text=self.cloud_user)
        if self.cloud_role == "admin":
            self.admin_report_btn.pack(fill="x", pady=5)
            self.create_user_btn.pack(fill="x", pady=5)
            self.topup_btn.grid(row=0, column=4, padx=(0, 18), sticky="e")
        stats = self.cloud.request("/me/stats")
        self._apply_me_stats(stats)
        quota_text = (f" • วันนี้ {self.daily_purchased}/{self.daily_limit} เบอร์"
                      if self.daily_limit else " • โควตารายวันไม่จำกัด")
        self.notice_var.set(f"เข้าสู่ระบบ: {self.cloud_user}{quota_text} • OTP สำเร็จเดือนนี้ {self.monthly_success}")
        self._run(self._fetch_history_counts, self._history_counts_loaded)
        if not self.update_checked:
            self.update_checked = True
            self.after(1800, lambda: self._check_for_updates(True))
        self.after_idle(self._show_main_window)

    @staticmethod
    def _clock_text(value):
        text = str(value or "").strip().replace("T", " ")
        if not text:
            return "—"
        clock = text.split()[-1]
        return clock[:5] if ":" in clock else text

    @staticmethod
    def _short_date(value):
        try:
            return datetime.fromisoformat(str(value).replace("T", " ")).strftime("%d/%m")
        except (TypeError, ValueError):
            return "—"

    @staticmethod
    def _cloud_datetime(value):
        try:
            return datetime.fromisoformat(str(value).replace("T", " "))
        except (TypeError, ValueError):
            return None

    def _daily_otp_summary(self):
        if not self.first_success_latest_cycle:
            return "ยังไม่มีประวัติ OTP สำเร็จ"
        cycle = self._short_date(self.latest_cycle_date)
        first = self._clock_text(self.first_success_latest_cycle)
        latest = self._clock_text(self.last_success_latest_cycle)
        end = self._clock_text(self.estimated_24h_end)
        end_date = self._short_date(self.estimated_24h_end)
        server_now = self._cloud_datetime(self.server_now_bangkok)
        window_end = self._cloud_datetime(self.estimated_24h_end)
        if server_now and window_end and server_now >= window_end:
            next_text = "ครบ 24 ชม.แล้ว • เริ่มรอบใหม่ได้"
        else:
            next_text = f"เริ่มรอบใหม่ประมาณ {end_date} {end}"
        return f"รอบล่าสุด {cycle}: {first}–{latest}\n{next_text}"

    def _apply_me_stats(self, stats):
        self.monthly_purchased = int(stats.get("monthly_purchased", 0))
        self.monthly_success = int(stats.get("monthly_success", 0))
        self.daily_limit = int(stats.get("daily_limit", 0) or 0)
        self.daily_purchased = int(stats.get("daily_purchased", 0) or 0)
        self.daily_success = int(stats.get("daily_success", 0) or 0)
        self.first_success_today = stats.get("first_success_today")
        self.last_success_today = stats.get("last_success_today")
        self.latest_cycle_date = stats.get("latest_cycle_date") or self.first_success_today
        self.first_success_latest_cycle = (stats.get("first_success_latest_cycle")
                                           or self.first_success_today)
        self.last_success_latest_cycle = (stats.get("last_success_latest_cycle")
                                          or self.last_success_today)
        self.estimated_24h_end = stats.get("estimated_24h_end")
        self.server_now_bangkok = stats.get("server_now_bangkok")
        if hasattr(self, "daily_otp_var"):
            self.daily_otp_var.set(self._daily_otp_summary())

    def _fetch_history_counts(self):
        return self.cloud.request("/activations/history?scope=all&limit=1&offset=0")

    def _history_counts_loaded(self, page):
        counts = page.get("counts", {}) if isinstance(page, dict) else {}
        self.history_counts = {
            "success": int(counts.get("success", 0) or 0),
            "all": int(counts.get("all", 0) or 0)
        }
        self._last_tab_counts = None
        self._update_tab_labels()

    def _load_history_page(self, reset=False):
        if not self.cloud_user:
            return
        if reset:
            self.history_page = 1
        self.history_loading = True
        self.history_request_id += 1
        request_id = self.history_request_id
        if hasattr(self.table, "clear"):
            self.table.clear()
        else:
            for item in self.table.get_children():
                self.table.delete(item)
        self._update_pagination()
        self.notice_var.set("กำลังโหลดประวัติ…")
        scope = "success" if self.table_filter == "success" else "all"
        search = self.search_var.get().strip()
        page = self.history_page
        offset = (page - 1) * self.history_page_size
        query = urllib.parse.urlencode({
            "scope": scope, "limit": self.history_page_size,
            "offset": offset, "search": search
        })
        def work():
            try:
                return self.cloud.request(f"/activations/history?{query}"), None
            except Exception as exc:
                return None, str(exc)
        self._run(work, lambda result: self._history_page_loaded(
            result, page, scope, search, request_id))

    def _history_page_loaded(self, result, requested_page, scope, search, request_id):
        if request_id != self.history_request_id:
            return
        self.history_loading = False
        page, error = result
        if error:
            self.notice_var.set(f"โหลดประวัติไม่สำเร็จ: {error}")
            self._update_pagination()
            return
        if (self.table_filter not in ("success", "all")
                or ("success" if self.table_filter == "success" else "all") != scope
                or self.search_var.get().strip() != search
                or self.history_page != requested_page):
            self._update_pagination()
            return
        items = page.get("items", []) if isinstance(page, dict) else []
        current_ids = []
        old_page_ids = set(self.history_page_ids)
        for item in items:
            aid = str(item.get("activation_id", ""))
            if not aid:
                continue
            current_ids.append(aid)
            if aid in self.orders and aid not in old_page_ids:
                self.orders[aid].setdefault("buyer", item.get("username") or self.cloud_user)
                self.orders[aid].setdefault("purchased_at", item.get("purchased_at") or "")
                continue
            received = bool(item.get("otp_received_at") or item.get("otp_code"))
            state = str(item.get("status") or ("success" if received else "active"))
            status = "ได้รับ OTP แล้ว" if received else {
                "cancelled": "ยกเลิกแล้ว", "expired": "หมดเวลา",
                "completed": "เสร็จสิ้น", "active": "ซื้อแล้ว"
            }.get(state, state)
            self.orders[aid] = {
                "phone": item.get("phone") or "—", "price": float(item.get("price") or 0),
                "remaining": 0, "status": status, "code": item.get("otp_code") or "—",
                "active": False, "in_working": False, "actionable": False, "history": True,
                "buyer": item.get("username") or "—", "purchased_at": item.get("purchased_at") or "",
                "outcome": "success" if received else state, "cloud_recorded": received
            }
        for aid in set(self.orders) - set(current_ids):
            order = self.orders.get(aid)
            if order and not order.get("active") and not order.get("in_working"):
                self.orders.pop(aid, None)
        self.history_page_ids = current_ids
        self.history_total = int(page.get("total", len(items)) or 0)
        counts = page.get("counts", {})
        self.history_counts = {
            "success": int(counts.get("success", self.history_counts.get("success", 0)) or 0),
            "all": int(counts.get("all", self.history_counts.get("all", 0)) or 0)
        }
        pages = max(1, (self.history_total + self.history_page_size - 1) // self.history_page_size)
        if self.history_page > pages:
            self.history_page = pages
            self._load_history_page()
            return
        self._last_tab_counts = None
        self._render_orders()
        self._save_orders()
        self.notice_var.set(f"โหลดประวัติหน้า {self.history_page} แล้ว • {len(current_ids)} รายการ")

    def _show_main_window(self):
        if not self.winfo_exists():
            return
        self.deiconify(); self.lift(); self.focus_force()
        if self.cloud_user:
            # Connect and populate price/balance immediately after Login.
            self.after(80, self.refresh)

    def _check_for_updates(self, silent=False):
        if not silent: self.notice_var.set("กำลังตรวจสอบอัปเดต…")
        def work():
            try: return self.update_manager.check(), None
            except Exception as exc: return None, str(exc)
        self._run(work, lambda result: self._update_check_result(result, silent))

    def _update_check_result(self, result, silent):
        manifest, error = result
        if error:
            if not silent: self._error(f"ตรวจสอบอัปเดตไม่สำเร็จ: {error}")
            return
        if not manifest.get("available"):
            if not silent:
                self.notice_var.set(f"โปรแกรมเป็นเวอร์ชันล่าสุดแล้ว (v{APP_VERSION})")
            return
        message = (f"เวอร์ชันปัจจุบัน   v{APP_VERSION}\n"
                   f"เวอร์ชันใหม่       v{manifest['version']}\n\n"
                   "พร้อมดาวน์โหลดและติดตั้งอัตโนมัติ")
        if self._themed_confirm("มีอัปเดตใหม่", message + "\n\nต้องการอัปเดตตอนนี้หรือไม่?"):
            self.notice_var.set("กำลังดาวน์โหลดอัปเดต…")
            self._run(lambda: (manifest, self.update_manager.download_verified(manifest)), self._update_downloaded)

    def _update_downloaded(self, result):
        manifest, path = result
        self.notice_var.set(f"ดาวน์โหลด v{manifest['version']} และตรวจ SHA-256 สำเร็จ")
        if self._themed_confirm("พร้อมติดตั้ง", "ดาวน์โหลดและตรวจสอบไฟล์สำเร็จ\nต้องการปิดโปรแกรมเพื่อติดตั้งและเปิดใหม่หรือไม่?"):
            try: self.update_manager.install_and_restart(path)
            except Exception as exc:
                messagebox.showerror("ติดตั้งไม่สำเร็จ", str(exc), parent=self); return
            self.destroy()

    def _show_admin_report(self):
        if not self.cloud or self.cloud_role != "admin": return
        colors = self.palette
        window = tk.Toplevel(self); window.title("รายงานผู้ใช้ • OTP24HR")
        window.configure(bg=colors["window"]); window.resizable(False, False)
        window.transient(self); window.grab_set()
        try: window.iconbitmap(resource_path("1.ico"))
        except tk.TclError: pass
        width, height = 980, 640
        window.geometry(f"{width}x{height}+{self.winfo_x()+(self.winfo_width()-width)//2}+{self.winfo_y()+(self.winfo_height()-height)//2}")
        panel = tk.Frame(window, bg=colors["window"], padx=26, pady=22)
        panel.pack(fill="both", expand=True)

        header = tk.Frame(panel, bg=colors["window"]); header.pack(fill="x", pady=(0, 16))
        title_box = tk.Frame(header, bg=colors["window"]); title_box.pack(side="left")
        tk.Label(title_box, text="รายงานผู้ใช้", bg=colors["window"], fg=colors["text"],
                 font=("Segoe UI", 21, "bold")).pack(anchor="w")
        subtitle_var = tk.StringVar(value="กำลังโหลดรายงาน…")
        tk.Label(title_box, textvariable=subtitle_var, bg=colors["window"], fg=colors["muted"],
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))
        month_nav = tk.Frame(header, bg=colors["window"]); month_nav.pack(side="right")
        previous_btn = ttk.Button(month_nav, text="‹  เดือนก่อน", width=13)
        previous_btn.pack(side="left")
        month_var = tk.StringVar(value="—")
        tk.Label(month_nav, textvariable=month_var, bg="#211943", fg="#d4c5ff",
                 font=("Segoe UI", 10, "bold"), width=16, padx=10, pady=8).pack(side="left", padx=8)
        next_btn = ttk.Button(month_nav, text="เดือนถัดไป  ›", width=13)
        next_btn.pack(side="left")

        summary = tk.Frame(panel, bg=colors["window"]); summary.pack(fill="x", pady=(0, 14))
        summary_vars = [tk.StringVar(value="—") for _ in range(4)]
        for index, (label, value_var, tone) in enumerate((("ผู้ใช้ทั้งหมด", summary_vars[0], colors["text"]),
                                                          ("ซื้อในเดือน", summary_vars[1], colors["text"]),
                                                          ("OTP สำเร็จ", summary_vars[2], colors["success"]),
                                                          ("อัตราสำเร็จ", summary_vars[3], "#bca4ff"))):
            box = tk.Frame(summary, bg=colors["panel"], padx=17, pady=13,
                           highlightthickness=1, highlightbackground=colors["border"])
            box.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 5, 0 if index == 3 else 5))
            tk.Label(box, text=label, bg=colors["panel"], fg=colors["muted"],
                     font=("Segoe UI", 9)).pack(anchor="w")
            tk.Label(box, textvariable=value_var, bg=colors["panel"], fg=tone,
                     font=("Segoe UI", 15, "bold")).pack(anchor="w", pady=(3, 0))
            summary.grid_columnconfigure(index, weight=1, uniform="summary")

        table_card = tk.Frame(panel, bg=colors["panel"], padx=14, pady=14,
                              highlightthickness=1, highlightbackground=colors["border"])
        table_card.pack(fill="both", expand=True)
        table_frame = tk.Frame(table_card, bg=colors["panel"]); table_frame.pack(fill="both", expand=True)
        table = ttk.Treeview(table_frame, columns=("user", "purchased", "success", "cycle", "first", "latest"),
                             show="headings", style="Report.Treeview", selectmode="browse", height=7)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=table.yview,
                                  style="Dark.Vertical.TScrollbar")
        table.configure(yscrollcommand=scrollbar.set)
        for column, title, size, stretch in (("user", "USERNAME", 150, True), ("purchased", "ซื้อในเดือน", 105, False),
                                             ("success", "OTP สำเร็จ", 105, False), ("cycle", "รอบล่าสุด", 90, False),
                                             ("first", "เวลาเริ่ม", 120, False), ("latest", "เวลาสิ้นสุด", 120, False)):
            table.heading(column, text=title)
            table.column(column, width=size, minwidth=90, anchor="center", stretch=stretch)
        table.pack(side="left", fill="both", expand=True); scrollbar.pack(side="right", fill="y")

        footer = tk.Frame(panel, bg=colors["window"]); footer.pack(fill="x", pady=(14, 0))
        updated_var = tk.StringVar(value="กำลังเชื่อมต่อ Cloudflare…")
        tk.Label(footer, textvariable=updated_var, bg=colors["window"], fg=colors["muted"],
                 font=("Segoe UI", 9)).pack(side="left")
        ttk.Button(footer, text="ปิดหน้าต่าง", command=window.destroy).pack(side="right")

        thai_months = ("", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
                       "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม")
        current_month = datetime.now().strftime("%Y-%m")
        state = {"month": current_month, "request": 0}

        def month_label(value):
            year, month = (int(part) for part in value.split("-"))
            return f"{thai_months[month]} {year + 543}"

        def shifted(value, delta):
            year, month = (int(part) for part in value.split("-"))
            index = year * 12 + month - 1 + delta
            return f"{index // 12:04d}-{index % 12 + 1:02d}"

        def apply_report(result):
            report, error, request_id = result
            if not window.winfo_exists() or request_id != state["request"]:
                return
            previous_btn.configure(state="normal")
            next_btn.configure(state="normal" if state["month"] < current_month else "disabled")
            if error:
                updated_var.set(f"โหลดรายงานไม่สำเร็จ • {error}")
                return
            users = list(report.get("users", []))
            selected = str(report.get("month") or state["month"])
            state["month"] = selected
            month_var.set(month_label(selected))
            subtitle_var.set(f"สรุปประจำเดือน {selected} • รอบ OTP ล่าสุดยังแสดงต่อหลังข้ามวัน")
            total_purchased = sum(int(row.get("monthly_purchased", 0)) for row in users)
            total_success = sum(int(row.get("monthly_success", 0)) for row in users)
            rate = (total_success * 100 / total_purchased) if total_purchased else 0
            for variable, value in zip(summary_vars, (f"{len(users):,} คน", f"{total_purchased:,} เบอร์",
                                                        f"{total_success:,} เบอร์", f"{rate:.1f}%")):
                variable.set(value)
            for item in table.get_children():
                table.delete(item)
            for row in users:
                table.insert("", "end", values=(row.get("username", "—"), row.get("monthly_purchased", 0),
                                                  row.get("monthly_success", 0), self._short_date(row.get("latest_cycle_date")),
                                                  self._clock_text(row.get("first_success_latest_cycle")),
                                                  self._clock_text(row.get("last_success_latest_cycle"))))
            updated_var.set(f"อัปเดตล่าสุด • {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")

        def load_month(value):
            if value > current_month:
                return
            state["month"] = value; state["request"] += 1
            request_id = state["request"]
            month_var.set(month_label(value)); subtitle_var.set(f"กำลังโหลดรายงาน {value}…")
            updated_var.set("กำลังดึงข้อมูลโดยไม่ทำให้หน้าต่างค้าง…")
            previous_btn.configure(state="disabled"); next_btn.configure(state="disabled")
            def work():
                try:
                    report = self.cloud.request("/admin/stats?" + urllib.parse.urlencode({"month": value}))
                    return report, None, request_id
                except Exception as exc:
                    return None, str(exc), request_id
            self._run(work, apply_report)

        previous_btn.configure(command=lambda: load_month(shifted(state["month"], -1)))
        next_btn.configure(command=lambda: load_month(shifted(state["month"], 1)))
        load_month(current_month)

    def _show_create_user(self):
        if self.cloud_role != "admin":
            return
        colors = self.palette
        window = tk.Toplevel(self); window.title("จัดการสมาชิก • OTP24HR")
        window.configure(bg=colors["window"]); window.resizable(False, False)
        window.transient(self); window.grab_set()
        try: window.iconbitmap(resource_path("1.ico"))
        except tk.TclError: pass
        width, height = 900, 640
        window.geometry(f"{width}x{height}+{self.winfo_x()+(self.winfo_width()-width)//2}+{self.winfo_y()+(self.winfo_height()-height)//2}")
        panel = tk.Frame(window, bg=colors["window"], padx=25, pady=22)
        panel.pack(fill="both", expand=True)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=1)
        header = tk.Frame(panel, bg=colors["window"]); header.grid(row=0, column=0, sticky="ew", pady=(0, 15))
        title_box = tk.Frame(header, bg=colors["window"]); title_box.pack(side="left")
        tk.Label(title_box, text="จัดการสมาชิก", bg=colors["window"], fg=colors["text"],
                 font=("Segoe UI", 21, "bold")).pack(anchor="w")
        tk.Label(title_box, text="แก้ไขบัญชี กำหนดโควตาซื้อต่อวัน และปิดบัญชีสมาชิก",
                 bg=colors["window"], fg=colors["muted"], font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))
        status_var = tk.StringVar(value="กำลังโหลดสมาชิก…")
        tk.Label(header, textvariable=status_var, bg=colors["window"], fg=colors["muted"],
                 font=("Segoe UI", 9)).pack(side="right", anchor="s")

        card = tk.Frame(panel, bg=colors["panel"], padx=13, pady=13,
                        highlightthickness=1, highlightbackground=colors["border"])
        card.grid(row=1, column=0, sticky="nsew")
        table_frame = tk.Frame(card, bg=colors["panel"]); table_frame.pack(fill="both", expand=True)
        columns = ("username", "role", "limit", "today", "month", "created")
        table = ttk.Treeview(table_frame, columns=columns, show="headings",
                             style="Report.Treeview", selectmode="browse", height=11)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=table.yview,
                                  style="Dark.Vertical.TScrollbar")
        table.configure(yscrollcommand=scrollbar.set)
        definitions = (
            ("username", "USERNAME", 180, "w"), ("role", "ROLE", 95, "center"),
            ("limit", "ลิมิต/วัน", 100, "center"), ("today", "ซื้อวันนี้", 100, "center"),
            ("month", "ซื้อเดือนนี้", 110, "center"), ("created", "สร้างเมื่อ", 185, "center")
        )
        for key, title, size, anchor in definitions:
            table.heading(key, text=title); table.column(key, width=size, anchor=anchor, stretch=key in ("username", "created"))
        table.pack(side="left", fill="both", expand=True); scrollbar.pack(side="right", fill="y")
        records = {}
        current_user_id = {"value": None}

        def selected_record():
            selected = table.selection()
            return records.get(selected[0]) if selected else None

        def refresh():
            try:
                result = self.cloud.request("/admin/users")
            except Exception as exc:
                status_var.set(f"โหลดสมาชิกไม่สำเร็จ: {exc}"); return
            records.clear(); current_user_id["value"] = str(result.get("current_user_id", ""))
            for item in table.get_children(): table.delete(item)
            for record in result.get("users", []):
                iid = str(record.get("id")); records[iid] = record
                limit = int(record.get("daily_limit", 0) or 0)
                table.insert("", "end", iid=iid, values=(
                    record.get("username", "—"), "Admin" if record.get("role") == "admin" else "User",
                    f"{limit} เบอร์" if limit else "ไม่จำกัด", record.get("purchased_today", 0),
                    record.get("purchased_month", 0), str(record.get("created_at") or "—").replace("T", " ")[:16]
                ))
            status_var.set(f"สมาชิกที่ใช้งานได้ {len(records)} บัญชี")

        def create_user():
            self._show_user_editor(window, None, refresh)

        def edit_user(_event=None):
            record = selected_record()
            if not record:
                status_var.set("กรุณาเลือกสมาชิกที่ต้องการแก้ไข"); return
            if str(record.get("id")) == current_user_id["value"]:
                status_var.set("บัญชีที่กำลังใช้งานให้เปลี่ยนรหัสผ่านจากเมนูตั้งค่า")
                return
            self._show_user_editor(window, record, refresh)

        def delete_user():
            record = selected_record()
            if not record:
                status_var.set("กรุณาเลือกสมาชิกที่ต้องการลบ"); return
            if str(record.get("id")) == current_user_id["value"]:
                status_var.set("ไม่สามารถลบบัญชีที่กำลังใช้งานได้"); return
            username = record.get("username", "—")
            if not messagebox.askyesno(
                "ยืนยันลบสมาชิก", f"ต้องการปิดบัญชี {username} ใช่หรือไม่?\n\nสมาชิกจะล็อกอินไม่ได้ แต่ประวัติซื้อเดิมจะไม่ถูกลบ",
                parent=window
            ):
                return
            try:
                self.cloud.request(f"/admin/users/{record['id']}", "DELETE")
            except Exception as exc:
                status_var.set(f"ลบสมาชิกไม่สำเร็จ: {exc}"); return
            status_var.set(f"ปิดบัญชี {username} เรียบร้อยแล้ว"); refresh()

        actions = tk.Frame(panel, bg=colors["window"], height=45)
        actions.grid(row=2, column=0, sticky="ew", pady=(14, 0)); actions.grid_propagate(False)
        ttk.Button(actions, text="＋ สร้างสมาชิก", command=create_user, style="Green.TButton").pack(side="left", fill="y")
        ttk.Button(actions, text="แก้ไขสมาชิก", command=edit_user).pack(side="left", fill="y", padx=8)
        ttk.Button(actions, text="ลบสมาชิก", command=delete_user, style="Danger.TButton").pack(side="left", fill="y")
        ttk.Button(actions, text="รีเฟรช", command=refresh).pack(side="right", fill="y", padx=(8, 0))
        ttk.Button(actions, text="ปิด", command=window.destroy).pack(side="right", fill="y")
        table.bind("<Double-1>", edit_user)
        refresh(); self._apply_dark_palette(window)

    def _show_user_editor(self, parent, record, on_saved):
        editing = record is not None
        window = tk.Toplevel(parent); window.title("แก้ไขสมาชิก" if editing else "สร้างสมาชิก")
        window.configure(bg="#061321"); window.resizable(False, False); window.transient(parent); window.grab_set()
        try: window.iconbitmap(resource_path("1.ico"))
        except tk.TclError: pass
        width, height = 500, 650
        window.geometry(f"{width}x{height}+{parent.winfo_x()+(parent.winfo_width()-width)//2}+{parent.winfo_y()+(parent.winfo_height()-height)//2}")
        panel = tk.Frame(window, bg="#0b1f36", padx=28, pady=23,
                         highlightthickness=1, highlightbackground="#294866")
        panel.pack(fill="both", expand=True, padx=16, pady=16)
        buttons = tk.Frame(panel, bg="#0b1f36", height=43); buttons.pack(fill="x", side="bottom", pady=(12, 0)); buttons.pack_propagate(False)
        ttk.Button(buttons, text="ยกเลิก", command=window.destroy).pack(side="right", fill="y")
        tk.Label(panel, text="แก้ไขรายละเอียดสมาชิก" if editing else "สร้างบัญชีสมาชิก",
                 bg="#0b1f36", fg="#eaf6ff", font=("Segoe UI", 19, "bold")).pack(anchor="w")
        tk.Label(panel, text="รหัสผ่านใหม่เว้นว่างได้หากไม่ต้องการเปลี่ยน" if editing else "กำหนดข้อมูลและโควตาการซื้อของสมาชิก",
                 bg="#0b1f36", fg="#8fa9c2", font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 16))
        username_var = tk.StringVar(value=str(record.get("username", "")) if editing else "")
        password_var, confirm_var = tk.StringVar(), tk.StringVar()
        daily_limit_var = tk.StringVar(value=str(int(record.get("daily_limit", 0) or 0)) if editing else "0")
        entries = []
        for label, variable, hidden in (("Username", username_var, False),
                                        ("รหัสผ่านใหม่" if editing else "Password", password_var, True),
                                        ("ยืนยันรหัสผ่าน", confirm_var, True)):
            tk.Label(panel, text=label, bg="#0b1f36", fg="#b8cbdd", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(5, 4))
            entry = ttk.Entry(panel, textvariable=variable, show="•" if hidden else "", font=("Segoe UI", 10))
            entry.pack(fill="x", ipady=4); entries.append(entry)
        tk.Label(panel, text="Role", bg="#0b1f36", fg="#b8cbdd", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(10, 4))
        role_var = tk.StringVar(value="ผู้ดูแลระบบ" if editing and record.get("role") == "admin" else "ผู้ใช้ทั่วไป")
        ctk.CTkSegmentedButton(
            panel, values=["ผู้ใช้ทั่วไป", "ผู้ดูแลระบบ"], variable=role_var,
            height=40, corner_radius=8, fg_color="#0c1222", unselected_color="#0c1222",
            unselected_hover_color="#252047", selected_color="#6d28d9",
            selected_hover_color="#7c3aed", text_color="#f5f3ff", border_width=1,
            font=ctk.CTkFont("Leelawadee UI", 12, "bold")
        ).pack(fill="x")
        tk.Label(panel, text="ลิมิตการซื้อต่อวัน", bg="#0b1f36", fg="#b8cbdd",
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(12, 4))
        ttk.Spinbox(panel, from_=0, to=1000, textvariable=daily_limit_var,
                    font=("Segoe UI", 10)).pack(fill="x", ipady=4)
        tk.Label(panel, text="ใส่ 0 = ไม่จำกัด • ระบบนับตามวันประเทศไทย และนับทุกเบอร์ที่ซื้อ",
                 bg="#0b1f36", fg="#8fa9c2", font=("Segoe UI", 8)).pack(anchor="w", pady=(4, 0))
        status = tk.Label(panel, text="", bg="#0b1f36", fg="#ff7b7b", font=("Segoe UI", 9), anchor="w")
        status.pack(fill="x", pady=(9, 0))

        def save():
            username, password = username_var.get().strip(), password_var.get()
            if len(username) < 3:
                status.configure(text="Username ต้องมีอย่างน้อย 3 ตัวอักษร"); return
            if (not editing or password) and len(password) < 8:
                status.configure(text="Password ต้องมีอย่างน้อย 8 ตัวอักษร"); return
            if password != confirm_var.get():
                status.configure(text="ยืนยัน Password ไม่ตรงกัน"); return
            try:
                daily_limit = int(daily_limit_var.get())
            except ValueError:
                daily_limit = -1
            if not 0 <= daily_limit <= 1000:
                status.configure(text="ลิมิตต่อวันต้องเป็นตัวเลข 0–1000"); return
            payload = {"username": username, "password": password,
                       "role": "admin" if role_var.get() == "ผู้ดูแลระบบ" else "user",
                       "daily_limit": daily_limit}
            try:
                if editing:
                    self.cloud.request(f"/admin/users/{record['id']}", "PATCH", payload)
                else:
                    self.cloud.request("/admin/users", "POST", payload)
            except Exception as exc:
                status.configure(text=str(exc)); return
            on_saved(); window.destroy()

        ttk.Button(buttons, text="บันทึกการแก้ไข" if editing else "สร้างบัญชี",
                   command=save, style="Green.TButton").pack(side="right", fill="y", padx=(0, 8))
        entries[0].focus_set(); self._apply_dark_palette(window)

    def _topup(self):
        if self.cloud_role != "admin": return
        if messagebox.askyesno("เติมเงิน", "ระบบจะเปิดหน้าชำระเงินภายนอกในเว็บเบราว์เซอร์\nต้องการดำเนินการต่อหรือไม่?", parent=self):
            webbrowser.open("https://hero-sms.com/", new=2)

    def _logout(self):
        if any(order.get("active") for order in self.orders.values()):
            messagebox.showwarning("ยังออกจากระบบไม่ได้", "กรุณาจัดการรายการที่กำลังรอ OTP ให้เสร็จก่อน", parent=self); return
        if not self._themed_confirm(
            "ออกจากระบบ",
            "ต้องการออกจากระบบและลบข้อมูลเข้าสู่ระบบที่จดจำไว้หรือไม่?\n\n"
            "คุณจะต้องเข้าสู่ระบบใหม่ก่อนใช้งานครั้งถัดไป"
        ): return
        self.saved_settings.pop("username", None); self.saved_settings.pop("password", None)
        try: self.credential_store.save(self.saved_settings)
        except OSError: pass
        self.cloud = CloudClient(CLOUD_API_URL)
        self.cloud_user = self.cloud_role = None
        self.user_badge.configure(text="—")
        self.monthly_purchased = self.monthly_success = 0
        self.daily_limit = self.daily_purchased = 0
        self.daily_success = 0
        self.first_success_today = self.last_success_today = None
        self.latest_cycle_date = None
        self.first_success_latest_cycle = self.last_success_latest_cycle = None
        self.estimated_24h_end = self.server_now_bangkok = None
        self.daily_otp_var.set(self._daily_otp_summary())
        self.history_page = 1
        self.history_total = 0
        self.history_counts = {"success": 0, "all": 0}
        self.history_page_ids = []
        self.history_request_id += 1
        self.history_loading = False
        self.admin_report_btn.pack_forget(); self.create_user_btn.pack_forget(); self.topup_btn.grid_forget()
        for item in self.table.get_children(): self.table.delete(item)
        self.orders.clear()
        self.notice_var.set("ออกจากระบบแล้ว")
        self.withdraw()
        self.after(100, self._require_login)

    def _show_settings(self):
        if not self.cloud_user: return
        try: stats = self.cloud.request("/me/stats")
        except Exception as exc:
            messagebox.showerror("โหลดข้อมูลไม่สำเร็จ", str(exc), parent=self); return
        self._apply_me_stats(stats)
        window = tk.Toplevel(self); window.title("ตั้งค่า")
        window.configure(bg="#061321"); window.resizable(False, False)
        try: window.iconbitmap(resource_path("1.ico"))
        except tk.TclError: pass
        width, height = 540, 610
        window.geometry(f"{width}x{height}+{self.winfo_x()+(self.winfo_width()-width)//2}+{self.winfo_y()+(self.winfo_height()-height)//2}")
        window.transient(self); window.grab_set()
        shell = tk.Frame(window, bg="#0b1f36", highlightthickness=1, highlightbackground="#294866")
        shell.pack(fill="both", expand=True, padx=16, pady=16)
        canvas = tk.Canvas(shell, bg="#0b1f36", highlightthickness=0)
        scrollbar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        panel = tk.Frame(canvas, bg="#0b1f36", padx=24, pady=20)
        panel_id = canvas.create_window((0, 0), window=panel, anchor="nw")
        panel.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(panel_id, width=e.width))
        window.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        tk.Label(panel, text="ตั้งค่าผู้ใช้", bg="#0b1f36", fg="#eaf6ff", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        tk.Label(panel, text=f"เข้าสู่ระบบเป็น {self.cloud_user}", bg="#0b1f36", fg="#8fa9c2",
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 14))

        stats_row = tk.Frame(panel, bg="#0b1f36"); stats_row.pack(fill="x", pady=(0, 15))
        for index, (label, value, color) in enumerate((("ซื้อเดือนนี้", self.monthly_purchased, "#eaf6ff"),
                                                       ("ได้รับ OTP สำเร็จ", self.monthly_success, "#58d6ff"))):
            box = tk.Frame(stats_row, bg="#102a46", padx=15, pady=11)
            box.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 5, 5 if index == 0 else 0))
            tk.Label(box, text=label, bg="#102a46", fg="#8fa9c2", font=("Segoe UI", 9)).pack(anchor="w")
            tk.Label(box, text=f"{value:,} เบอร์", bg="#102a46", fg=color,
                     font=("Segoe UI", 16, "bold")).pack(anchor="w", pady=(2, 0))
        stats_row.grid_columnconfigure(0, weight=1); stats_row.grid_columnconfigure(1, weight=1)

        def entry_shortcut(event):
            action = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 65: "<<SelectAll>>"}.get(event.keycode)
            if not action: return None
            if action == "<<SelectAll>>": event.widget.selection_range(0, "end"); event.widget.icursor("end")
            else: event.widget.event_generate(action)
            return "break"
        tk.Label(panel, text="ระบบ API เชื่อมต่อผ่านคลาวด์อย่างปลอดภัย",
                 bg="#102a46", fg="#58d6ff", font=("Segoe UI", 9, "bold"), padx=12, pady=9).pack(fill="x", pady=(0, 15))
        tk.Frame(panel, bg="#294866", height=1).pack(fill="x", pady=(0, 13))
        tk.Label(panel, text="เปลี่ยนรหัสผ่าน", bg="#0b1f36", fg="#eaf6ff",
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 8))
        current_var, new_var, confirm_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        for label, variable in (("รหัสผ่านปัจจุบัน", current_var), ("รหัสผ่านใหม่", new_var), ("ยืนยันรหัสผ่านใหม่", confirm_var)):
            tk.Label(panel, text=label, bg="#0b1f36", fg="#8fa9c2", font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 3))
            entry = ttk.Entry(panel, textvariable=variable, show="•", font=("Segoe UI", 10))
            entry.pack(fill="x", ipady=3); entry.bind("<Control-KeyPress>", entry_shortcut)
        password_status = tk.Label(panel, text="", bg="#0b1f36", fg="#ff7b7b", font=("Segoe UI", 9), anchor="w")
        password_status.pack(fill="x", pady=(6, 0))
        def change_password():
            current, new, confirm = current_var.get(), new_var.get(), confirm_var.get()
            if len(new) < 8:
                password_status.configure(text="รหัสผ่านใหม่ต้องมีอย่างน้อย 8 ตัวอักษร", fg="#ff7b7b"); return
            if new != confirm:
                password_status.configure(text="ยืนยันรหัสผ่านใหม่ไม่ตรงกัน", fg="#ff7b7b"); return
            try: self.cloud.request("/auth/change-password", "POST", {"current_password": current, "new_password": new})
            except Exception as exc:
                password_status.configure(text=str(exc), fg="#ff7b7b"); return
            if self.saved_settings.get("username") == self.cloud_user:
                self.saved_settings["password"] = new; self.credential_store.save(self.saved_settings)
            current_var.set(""); new_var.set(""); confirm_var.set("")
            password_status.configure(text="เปลี่ยนรหัสผ่านเรียบร้อยแล้ว", fg="#58d6ff")
            messagebox.showinfo("เปลี่ยนรหัสผ่านสำเร็จ", "เปลี่ยนรหัสผ่านเรียบร้อยแล้ว", parent=window)
        ttk.Button(panel, text="เปลี่ยนรหัสผ่าน", command=change_password,
                   style="Green.TButton").pack(anchor="w", pady=(5, 0))
        self._apply_dark_palette(window)

    def _show_login_dialog(self):
        dialog = tk.Toplevel(self)
        dialog.title("เข้าสู่ระบบ OTP24HR")
        dialog.configure(bg="#061321")
        dialog.resizable(False, False)
        try: dialog.iconbitmap(resource_path("1.ico"))
        except tk.TclError: pass
        width, height = 430, 470
        x = max(0, (dialog.winfo_screenwidth() - width) // 2)
        y = max(0, (dialog.winfo_screenheight() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        if self.state() != "withdrawn":
            dialog.transient(self)
        dialog.attributes("-topmost", True)
        dialog.after(250, lambda: dialog.winfo_exists() and dialog.attributes("-topmost", False))
        dialog.grab_set()
        result = {"value": None}

        panel = tk.Frame(dialog, bg="#0b1f36", padx=30, pady=25, highlightthickness=1,
                         highlightbackground="#294866")
        panel.pack(fill="both", expand=True, padx=18, pady=18)
        buttons = tk.Frame(panel, bg="#0b1f36", height=44)
        buttons.pack(fill="x", side="bottom", pady=(14, 0))
        buttons.pack_propagate(False)
        tk.Label(panel, text="เข้าสู่ระบบ", bg="#0b1f36", fg="#eaf6ff",
                 font=("Segoe UI", 21, "bold")).pack(anchor="w")
        tk.Label(panel, text="กรอกบัญชีที่ผู้ดูแลระบบสร้างให้คุณ", bg="#0b1f36", fg="#8fa9c2",
                 font=("Segoe UI", 10)).pack(anchor="w", pady=(2, 20))

        tk.Label(panel, text="ชื่อผู้ใช้", bg="#0b1f36", fg="#b8cbdd",
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 5))
        username_var = tk.StringVar(value=str(self.saved_settings.get("username", "")))
        username_entry = ttk.Entry(panel, textvariable=username_var, font=("Segoe UI", 11))
        username_entry.pack(fill="x", ipady=5)
        tk.Label(panel, text="รหัสผ่าน", bg="#0b1f36", fg="#b8cbdd",
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(13, 5))
        password_var = tk.StringVar(value=str(self.saved_settings.get("password", "")))
        password_entry = ttk.Entry(panel, textvariable=password_var, show="•", font=("Segoe UI", 11))
        password_entry.pack(fill="x", ipady=5)

        def submit(_event=None):
            username, password = username_var.get().strip(), password_var.get()
            if not username or not password:
                error_label.configure(text="กรุณากรอกชื่อผู้ใช้และรหัสผ่าน")
                return
            result["value"] = (username, password, remember_var.get()); dialog.destroy()

        def cancel(_event=None): dialog.destroy()
        def shortcut(event):
            action = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 65: "<<SelectAll>>"}.get(event.keycode)
            if not action: return None
            widget = event.widget
            if action == "<<SelectAll>>": widget.selection_range(0, "end"); widget.icursor("end")
            else: widget.event_generate(action)
            return "break"
        for entry in (username_entry, password_entry):
            entry.bind("<Control-KeyPress>", shortcut)
            entry.bind("<Return>", submit)

        error_label = tk.Label(panel, text="", bg="#0b1f36", fg="#ff7b7b",
                               font=("Segoe UI", 9), anchor="w")
        error_label.pack(fill="x", pady=(7, 0))
        remember_var = tk.BooleanVar(value=True)
        tk.Checkbutton(panel, text="จดจำการเข้าสู่ระบบในเครื่องนี้", variable=remember_var,
                       bg="#0b1f36", fg="#b8cbdd", selectcolor="#102a46", activebackground="#0b1f36",
                       activeforeground="#eaf6ff", font=("Segoe UI", 9)).pack(anchor="w", pady=(3, 0))
        ttk.Button(buttons, text="ยกเลิก", command=cancel).pack(side="right", fill="y")
        ttk.Button(buttons, text="เข้าสู่ระบบ", command=submit,
                   style="Green.TButton").pack(side="right", fill="y", padx=(0, 9))
        dialog.bind("<Escape>", cancel)
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        username_entry.focus_force()
        self._apply_dark_palette(dialog)
        self.wait_window(dialog)
        return result["value"]

    def _api_key_shortcut(self, event):
        # Windows keycodes stay the same even when the active layout is Thai.
        action = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 65: "<<SelectAll>>"}.get(event.keycode)
        if not action:
            return None
        if action == "<<SelectAll>>":
            self.key_entry.selection_range(0, "end")
            self.key_entry.icursor("end")
        else:
            self.key_entry.event_generate(action)
        return "break"

    def _paste_api_key(self, _event=None):
        try:
            value = self.clipboard_get()
        except tk.TclError:
            return "break"
        if self.key_entry.selection_present():
            self.key_entry.delete("sel.first", "sel.last")
        self.key_entry.insert("insert", value)
        return "break"

    def _show_key_menu(self, event):
        self.key_entry.focus_set()
        self.key_menu.tk_popup(event.x_root, event.y_root)

    def _client(self): return HeroClient(cloud=self.cloud)

    def _run(self, work, success=None):
        def runner():
            try: result = work()
            except Exception as exc: self.jobs.put((self._error, (str(exc),)))
            else:
                if success: self.jobs.put((success, (result,)))
        threading.Thread(target=runner, daemon=True).start()

    def _drain_jobs(self):
        processed = 0
        try:
            while processed < 50:
                fn, args = self.jobs.get_nowait(); fn(*args)
                processed += 1
        except queue.Empty: pass
        self.after(20 if processed else 60, self._drain_jobs)

    def _error(self, message):
        # A failed balance refresh must not leave the guard latched forever.
        self._balance_refresh_pending = False
        self.notice_var.set(message); self.notice.configure(fg="#b42318")
        self.buy_btn.configure(state="normal" if self.quote is not None else "disabled")

    def refresh(self):
        self.notice_var.set("กำลังอัปเดตราคาและยอดเงิน…"); self.notice.configure(fg="#3b4b40")
        self.buy_btn.configure(state="disabled")
        client = self._client()
        self._run(lambda: (client.offers(), client.balance(), client.usd_thb()), self._refreshed)

    def _refreshed(self, result):
        offers, balance, (rate, rate_date) = result
        self.quote = offers[0][0] if offers else None; self.fx_rate = rate; self.offer_rows = offers
        stock = sum(x[1] for x in offers)
        if self.quote is None: self.price_var.set("ไม่มีสินค้า")
        elif rate is None: self.price_var.set(f"${self.quote:.4f}")
        else: self.price_var.set(f"฿{self.quote*rate:.2f}")
        self.balance_var.set(f"฿{balance*rate:,.2f}" if rate is not None else f"${balance:.4f}")
        self.stock_var.set(f"คงเหลือ {stock:,} เบอร์")
        rate_text = f" • เรต {rate_date}" if rate_date else ""
        if self.cloud_user and self.daily_limit:
            cloud_stats = f" • วันนี้ {self.daily_purchased}/{self.daily_limit} เบอร์ • OTP สำเร็จเดือนนี้ {self.monthly_success}"
        else:
            cloud_stats = f" • OTP สำเร็จเดือนนี้ {self.monthly_success} เบอร์" if self.cloud_user else ""
        self.notice_var.set(f"พร้อมใช้งาน • อัปเดต {datetime.now().strftime('%H:%M:%S')}{rate_text}{cloud_stats}")
        self.buy_btn.configure(state="normal" if self.quote is not None and stock else "disabled")

    def _refresh_balance_only(self):
        """Refresh just the wallet after an order is finished without reloading offers/FX."""
        if self._balance_refresh_pending:
            return
        self._balance_refresh_pending = True
        self._run(lambda: self._client().balance(), self._balance_refreshed)

    def _balance_refreshed(self, balance):
        self._balance_refresh_pending = False
        self.balance_var.set(f"฿{balance*self.fx_rate:,.2f}" if self.fx_rate is not None else f"${balance:.4f}")
        self.notice_var.set(f"เสร็จสิ้นแล้ว • อัปเดตยอดเงิน {datetime.now().strftime('%H:%M:%S')}")

    def buy(self):
        if self.quote is None: return
        qty = max(1, min(5, int(self.qty_var.get())))
        if self.cloud:
            try:
                self.cloud.request("/queue/acquire", "POST", {"quantity": qty})
            except Exception as exc:
                messagebox.showwarning("มีผู้ใช้อื่นกำลังซื้อ", str(exc), parent=self); return
        unit_price = (f"฿{self.quote * self.fx_rate:.2f}" if self.fx_rate is not None
                      else f"${self.quote:.4f}")
        total_price = (f"฿{self.quote * self.fx_rate * qty:.2f}" if self.fx_rate is not None
                       else f"${self.quote * qty:.4f}")
        confirm_message = (f"บริการ                 LINE ประเทศไทย\n"
                           f"จำนวน                  {qty} เบอร์\n"
                           f"ราคาเริ่มต้น/เบอร์      {unit_price}\n"
                           f"ยอดรวมโดยประมาณ        {total_price}")
        if not self._themed_confirm("ยืนยันการซื้อหมายเลข", confirm_message):
            if self.cloud:
                try: self.cloud.request("/queue/release", "POST", {})
                except Exception: pass
            return
        self.buy_btn.configure(state="disabled"); self.notice_var.set(f"กำลังซื้อ {qty} เบอร์…")
        client, price = self._client(), self.quote
        def work():
            bought, errors = [], []
            for _ in range(qty):
                try:
                    aid, phone, paid = client.buy(price)
                    # Record the purchase before any follow-up request so a paid
                    # activation can never disappear if setStatus fails.
                    item = {"aid": aid, "phone": phone, "price": paid, "ready_error": None}
                    bought.append(item)
                    try:
                        client.request("setStatus", id=aid, status=1)
                    except Exception as exc:
                        item["ready_error"] = str(exc)
                        errors.append(f"{phone}: {exc}")
                except Exception as exc:
                    errors.append(str(exc)); break
            return bought, errors
        self._run(work, self._bought)

    def _bought(self, result):
        bought, errors = result
        if bought and self.cloud_user:
            self.daily_purchased += len(bought)
            self.monthly_purchased += len(bought)
        for item in bought:
            aid, phone, price = item["aid"], item["phone"], item["price"]
            status = "กำลังรอ SMS…" if not item["ready_error"] else "ซื้อแล้ว • รอตรวจสถานะ"
            self.orders[aid] = {"phone": "+" + phone.lstrip("+"), "price": price,
                                "remaining": 1200, "status": status, "code": "—", "active": True,
                                "in_working": True, "actionable": True, "history": False,
                                "buyer": self.cloud_user or "local",
                                "purchased_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "outcome": "active", "cloud_recorded": False}
            self.table.insert("", "end", iid=aid)
            self._sync_row(aid)
        if bought:
            self.table.selection_set(bought[-1]["aid"]); self._start_timers(); self._save_orders()
        error_text = " • ".join(errors)
        self.notice_var.set(f"ซื้อสำเร็จ {len(bought)} เบอร์" + (f" • {error_text}" if errors else ""))
        self.notice.configure(fg="#ff7b7b" if errors else "#58d6ff")
        self.buy_btn.configure(state="normal" if self.quote is not None else "disabled")
        if self.cloud:
            for item in bought:
                self._run(lambda item=item: self.cloud.request("/activations/register", "POST", {
                    "activation_id": item["aid"], "phone": item["phone"], "price": item["price"]
                }), lambda result, item=item: self._cloud_registered(item["aid"], result))
            self._run(lambda: self.cloud.request("/queue/release", "POST", {}))

    def _cloud_registered(self, aid, result):
        warnings = []
        if result.get("duplicate_count_7d"):
            warnings.append(f"เคยได้รับ OTP {result['duplicate_count_7d']} ครั้งใน 7 วัน")
        report = result.get("block_report")
        if report:
            try:
                until = datetime.strptime(report["blocked_until"], "%Y-%m-%d %H:%M:%S")
                remaining = max(0, (until - datetime.utcnow()).days + 1)
                warnings.append(f"มีรายงานติดลิมิต {report['blocked_days']} วัน • เหลือประมาณ {remaining} วัน")
            except (TypeError, ValueError):
                warnings.append(f"มีรายงานติดลิมิต {report['blocked_days']} วัน ถึง {report['blocked_until']}")
        if warnings and aid in self.orders:
            self.orders[aid]["status"] = "⚠ " + " • ".join(warnings)
            self._sync_row(aid); self._save_orders()
            messagebox.showwarning("พบประวัติเบอร์", f"{self.orders[aid]['phone']}\n" + "\n".join(warnings), parent=self)

    def _selected_id(self):
        selected = self.table.selection(); return selected[0] if selected else None

    def poll_selected(self):
        aid = self._selected_id()
        if aid and self.orders.get(aid, {}).get("actionable", True):
            self._poll_ids([aid])

    def _poll_ids(self, ids):
        ids = [aid for aid in ids if aid in self.orders and aid not in self.polling_ids
               and self.orders[aid].get("actionable", True)]
        if not ids:
            return
        self.polling_ids.update(ids)
        client = self._client()
        def work():
            results = []
            for aid in ids:
                try: results.append((aid, client.request("getStatus", id=aid), None))
                except Exception as exc: results.append((aid, None, str(exc)))
            return results
        self._run(work, self._polled)

    def _polled(self, results):
        for aid, raw, error in results:
            self.polling_ids.discard(aid)
            if aid not in self.orders: continue
            if error:
                if "ยกเลิก" in error:
                    self._set_local_history_status(aid, "cancelled")
                    continue
                self.orders[aid]["status"] = f"ตรวจไม่สำเร็จ: {error}"
                self._sync_row(aid)
                continue
            state, _, value = str(raw).partition(":")
            if state == "STATUS_OK":
                first_receipt = self.orders[aid].get("code") != value
                self.orders[aid].update(status="ได้รับ OTP แล้ว", code=value, active=False,
                                        in_working=True, outcome="success")
                if self.cloud and not self.orders[aid].get("cloud_recorded"):
                    self._record_cloud_success(aid)
                if first_receipt:
                    self._notify_otp(self.orders[aid]["phone"], value)
            else:
                self.orders[aid]["status"] = ERRORS.get(state, str(raw))
                if state == "STATUS_CANCEL":
                    self._set_local_history_status(aid, "cancelled")
                    continue
            self._sync_row(aid)
        self._save_orders()

    def _record_cloud_success(self, aid):
        if aid in self.cloud_success_pending or aid not in self.orders: return
        self.cloud_success_pending.add(aid)
        def work():
            try:
                self.cloud.request("/activations/success", "POST", {
                    "activation_id": aid, "otp_code": self.orders[aid].get("code", "")
                })
                return None
            except Exception as exc:
                return str(exc)
        self._run(work, lambda error: self._cloud_success_result(aid, error))

    def _cloud_success_result(self, aid, error):
        self.cloud_success_pending.discard(aid)
        if aid not in self.orders: return
        if error:
            self.after(10000, lambda: self._record_cloud_success(aid))
            return
        if not self.orders[aid].get("cloud_recorded"):
            self.orders[aid]["cloud_recorded"] = True
            self.monthly_success += 1
        self._run(lambda: self.cloud.request("/me/stats"), self._apply_me_stats)
        self._save_orders()

    def _record_cloud_status(self, aid, status):
        if not self.cloud:
            return
        self._run(lambda: self.cloud.request("/activations/status", "POST", {
            "activation_id": aid, "status": status
        }))

    def _set_local_history_status(self, aid, state):
        if aid not in self.orders:
            return
        labels = {"cancelled": "ยกเลิกแล้ว", "expired": "หมดเวลา", "completed": "เสร็จสิ้น"}
        order = self.orders[aid]
        order.update(status=labels.get(state, state), active=False, in_working=False,
                     actionable=False, history=True, outcome=state)
        self.polling_ids.discard(aid)
        self._record_cloud_status(aid, state)
        self._sync_row(aid)

    def command_selected(self, name):
        aid = self._selected_id()
        if not aid: return
        if not self.orders.get(aid, {}).get("actionable", True):
            self.notice_var.set("รายการนี้เป็นประวัติแล้ว • ใช้คลิกขวาเพื่อคัดลอกหรือรายงานเบอร์")
            return
        if name == "cancel" and not self._themed_confirm("ยืนยันการยกเลิก", "ยืนยันยกเลิกหมายเลขที่เลือก?"): return
        if name == "resend" and not self._themed_confirm("ขอ OTP ซ้ำ", "ต้องการรอรับ OTP ข้อความถัดไปจากหมายเลขนี้หรือไม่?"): return
        status = {"complete": 6, "cancel": 8, "resend": 3}[name]
        self._run(lambda: self._client().request("setStatus", id=aid, status=status),
                  lambda _: self._commanded(aid, name))

    def _commanded(self, aid, name):
        if aid in self.orders:
            if name == "cancel":
                self._set_local_history_status(aid, "cancelled")
            elif name == "complete":
                order = self.orders[aid]
                received = self._order_is_success(order)
                order.update(active=False, in_working=False, actionable=False, history=True,
                             outcome="success" if received else "completed")
                if not received:
                    order["status"] = "เสร็จสิ้น"
                self.polling_ids.discard(aid)
                self._record_cloud_status(aid, "completed")
                self._sync_row(aid)
                self._refresh_balance_only()
            else:
                self.orders[aid].update(status="ขอ OTP ซ้ำแล้ว • กำลังรอ SMS…", code="—", active=True,
                                        in_working=True, actionable=True, outcome="active")
                self._record_cloud_status(aid, "active")
                self._sync_row(aid)
                self._start_timers()
                self.notice_var.set(f"กำลังรอ OTP ข้อความถัดไปของ {self.orders[aid]['phone']}")
            self._save_orders()

    def _notify_otp(self, phone, code):
        try:
            winsound.PlaySound("SystemNotification", winsound.SND_ALIAS | winsound.SND_ASYNC)
        except (RuntimeError, OSError):
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        self.notice_var.set(f"OTP เข้าแล้ว • {phone} • {code}")
        try:
            self.bell()
            ctypes.windll.user32.FlashWindow(self.winfo_id(), True)
        except (tk.TclError, OSError):
            pass

    def _sync_row(self, aid):
        if aid not in self.orders:
            return
        order = self.orders[aid]
        paged_history = self.table_filter in ("success", "all") and bool(self.cloud_user)
        visible = self._order_matches_filter(order) and (not paged_history or aid in self.history_page_ids)
        if not visible:
            if self.table.exists(aid):
                self.table.delete(aid)
            self._update_tab_labels()
            self._update_action_bar()
            return
        if not self.table.exists(aid):
            self.table.insert("", "end", iid=aid)
        minutes, seconds = divmod(max(0, int(order.get("remaining", 0))), 60)
        if order.get("active") or order.get("in_working"):
            time_text = f"{minutes:02d}:{seconds:02d}"
        else:
            time_text = str(order.get("purchased_at") or "—").replace("T", " ")[:16]
        buyer = str(order.get("buyer") or "—")
        status_text = str(order.get("status") or "—")
        if buyer != "—":
            status_text = f"{status_text} • {buyer}"
        self.table.item(aid, values=(order["phone"], time_text,
                                    status_text, order.get("code", "—"), "•••"))
        self._update_tab_labels()

    def _start_timers(self):
        if self.timer_job is None: self.timer_job = self.after(1000, self._tick)
        if self.poll_job is None: self.poll_job = self.after(POLL_MS, self._auto_poll)

    def _tick(self):
        active = False
        for aid, order in self.orders.items():
            if order["active"]:
                order["remaining"] = max(0, order["remaining"] - 1)
                if order["remaining"] == 0:
                    self._set_local_history_status(aid, "expired")
                    self._save_orders()
                else:
                    active = True
                self._sync_row(aid)
        self.timer_job = self.after(1000, self._tick) if active else None

    def _auto_poll(self):
        ids = [aid for aid, order in self.orders.items() if order["active"]]
        if ids:
            if self.cloud_user or len(self.key_var.get().strip()) >= 8:
                self._poll_ids(ids)
            self.poll_job = self.after(POLL_MS, self._auto_poll)
        else: self.poll_job = None

    def _show_order_menu(self, event, row_override=None):
        row = row_override or self.table.identify_row(event.y)
        if not row: return
        self.table.selection_set(row); self.table.focus(row)
        popup = tk.Toplevel(self); popup.overrideredirect(True); popup.configure(bg="#7c3aed")
        popup.geometry(f"230x190+{event.x_root}+{event.y_root}")
        frame = tk.Frame(popup, bg="#100b20", padx=6, pady=6, highlightthickness=1, highlightbackground="#7c3aed")
        frame.pack(fill="both", expand=True)
        def action(fn): popup.destroy(); fn()
        for text, fn in (("คัดลอกหมายเลข", self._copy_selected), ("รายงานติดลิมิต 7 วัน", lambda: self._report_selected(7)),
                         ("รายงานติดลิมิต 20 วัน", lambda: self._report_selected(20)), ("กำหนดจำนวนวัน…", lambda: self._report_selected(None))):
            tk.Button(frame, text=text, command=lambda fn=fn: action(fn), anchor="w", relief="flat",
                      bg="#100b20", fg="#f5f3ff", activebackground="#6d28d9", activeforeground="#ffffff",
                      font=("Segoe UI", 9), padx=12, pady=6).pack(fill="x")
        popup.bind("<FocusOut>", lambda _e: popup.destroy())
        popup.focus_force()

    def _copy_selected(self, event=None):
        aid = self._selected_id()
        if aid:
            self.clipboard_clear(); self.clipboard_append(self.orders[aid]["phone"])
            self.notice_var.set(f"คัดลอก {self.orders[aid]['phone']} แล้ว")

    def _report_selected(self, days):
        aid = self._selected_id()
        if not aid: return
        if not self.cloud:
            messagebox.showwarning("ยังไม่ได้เชื่อม Cloudflare", "ต้อง Deploy Cloud API ก่อนจึงจะรายงานได้", parent=self); return
        if days is None:
            days = self._themed_days_dialog()
            if days is None: return
        phone = self.orders[aid]["phone"]
        if not self._themed_confirm("ยืนยันรายงาน", f"รายงาน {phone}\nว่าติดลิมิต {days} วัน?"): return
        self._run(lambda: self.cloud.request("/numbers/report", "POST", {"phone": phone, "days": days}),
                  lambda _: self.notice_var.set(f"รายงาน {phone} ติดลิมิต {days} วันแล้ว"))

    def _themed_confirm(self, title, message):
        colors = self.palette
        result = {"value": False}; window = tk.Toplevel(self); window.title(title)
        window.configure(bg=colors["window"]); window.resizable(False, False); window.transient(self); window.grab_set()
        try: window.iconbitmap(resource_path("1.ico"))
        except tk.TclError: pass
        width, height = 500, 340
        window.geometry(f"{width}x{height}+{self.winfo_x()+(self.winfo_width()-width)//2}+{self.winfo_y()+(self.winfo_height()-height)//2}")
        panel = tk.Frame(window, bg=colors["panel"], padx=25, pady=22,
                         highlightthickness=1, highlightbackground=colors["border"])
        panel.pack(fill="both", expand=True, padx=18, pady=18)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=1)
        heading = tk.Frame(panel, bg=colors["panel"])
        heading.grid(row=0, column=0, sticky="ew")
        tk.Label(heading, text="✓", bg="#2d1d5b", fg="#c4a7ff", font=("Segoe UI", 16, "bold"),
                 width=3, height=1).pack(side="left")
        heading_text = tk.Frame(heading, bg=colors["panel"]); heading_text.pack(side="left", padx=(12, 0))
        tk.Label(heading_text, text=title, bg=colors["panel"], fg=colors["text"],
                 font=("Segoe UI", 16, "bold")).pack(anchor="w")
        tk.Label(heading_text, text="โปรดตรวจสอบข้อมูลก่อนยืนยัน", bg=colors["panel"], fg=colors["muted"],
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))
        details = tk.Frame(panel, bg="#0c1222", padx=17, pady=14,
                           highlightthickness=1, highlightbackground=colors["border"])
        details.grid(row=1, column=0, sticky="nsew", pady=(17, 14))
        tk.Label(details, text=message, bg="#0c1222", fg="#dbe0ef", font=("Segoe UI", 10),
                 justify="left", anchor="nw", wraplength=400).pack(fill="both", expand=True)
        buttons = tk.Frame(panel, bg=colors["panel"], height=43)
        buttons.grid(row=2, column=0, sticky="ew"); buttons.grid_propagate(False)
        def finish(value): result["value"] = value; window.destroy()
        ttk.Button(buttons, text="ยกเลิก", command=lambda: finish(False)).pack(side="right", fill="y")
        ttk.Button(buttons, text="ยืนยัน", command=lambda: finish(True), style="Green.TButton").pack(side="right", fill="y", padx=(0, 9))
        window.bind("<Return>", lambda _e: finish(True)); window.bind("<Escape>", lambda _e: finish(False))
        self.wait_window(window); return result["value"]

    def _themed_days_dialog(self):
        result = {"value": None}; window = tk.Toplevel(self); window.title("กำหนดจำนวนวัน")
        window.configure(bg="#070510"); window.resizable(False, False); window.transient(self); window.grab_set()
        width, height = 400, 300
        window.geometry(f"{width}x{height}+{self.winfo_x()+(self.winfo_width()-width)//2}+{self.winfo_y()+(self.winfo_height()-height)//2}")
        panel = tk.Frame(window, bg="#100b20", padx=25, pady=22, highlightthickness=1, highlightbackground="#7c3aed")
        panel.pack(fill="both", expand=True, padx=14, pady=14)
        buttons = tk.Frame(panel, bg="#100b20", height=42); buttons.pack(fill="x", side="bottom", pady=(12, 0))
        buttons.pack_propagate(False)
        tk.Label(panel, text="รายงานติดลิมิต", bg="#100b20", fg="#f5f3ff", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(panel, text="ติดลิมิตกี่วัน? (1–365)", bg="#100b20", fg="#cfc3e6", font=("Segoe UI", 9)).pack(anchor="w", pady=(8, 5))
        value = tk.StringVar(); entry = ttk.Entry(panel, textvariable=value, font=("Segoe UI", 11)); entry.pack(fill="x", ipady=4)
        status = tk.Label(panel, text="", bg="#100b20", fg="#ff7b7b", font=("Segoe UI", 9)); status.pack(anchor="w")
        def submit():
            try: days = int(value.get())
            except ValueError: days = 0
            if not 1 <= days <= 365: status.configure(text="กรุณากรอกตัวเลข 1–365"); return
            result["value"] = days; window.destroy()
        ttk.Button(buttons, text="ยกเลิก", command=window.destroy).pack(side="right", fill="y")
        ttk.Button(buttons, text="ตกลง", command=submit, style="Green.TButton").pack(side="right", fill="y", padx=(0, 8))
        entry.bind("<Return>", lambda _e: submit()); entry.focus_set(); self.wait_window(window)
        return result["value"]

    def _save_orders(self, immediate=False):
        if not self.orders_file: return
        if immediate:
            if self._save_orders_job:
                try: self.after_cancel(self._save_orders_job)
                except tk.TclError: pass
                self._save_orders_job = None
            self._write_orders()
        elif self._save_orders_job is None:
            self._save_orders_job = self.after(250, self._write_orders)

    def _write_orders(self):
        self._save_orders_job = None
        if not self.orders_file: return
        try:
            os.makedirs(os.path.dirname(self.orders_file), exist_ok=True)
            payload = {"saved_at": time.time(), "orders": self.orders}
            with open(self.orders_file, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _load_orders(self):
        if not self.orders_file: return
        try:
            with open(self.orders_file, "r", encoding="utf-8") as stream:
                payload = json.load(stream)
            elapsed = max(0, int(time.time() - float(payload.get("saved_at", time.time()))))
            loaded = payload.get("orders", {})
            if not isinstance(loaded, dict):
                return
            pending_success = []
            for aid, order in loaded.items():
                if not isinstance(order, dict) or not order.get("phone"):
                    continue
                status = str(order.get("status", ""))
                order.setdefault("price", 0.0); order.setdefault("code", "—")
                order.setdefault("cloud_recorded", False)
                order.setdefault("status", "ไม่ทราบสถานะ"); order.setdefault("active", False)
                success = (order.get("code") not in (None, "", "—") or "ได้รับ OTP" in status
                           or order.get("cloud_recorded") is True)
                final = any(text in status for text in ("ยกเลิก", "เสร็จสิ้น", "หมดเวลา"))
                order.setdefault("in_working", bool(order["active"] or (success and not final)))
                order.setdefault("history", final)
                order.setdefault("actionable", not final)
                order.setdefault("buyer", self.cloud_user or "local")
                order.setdefault("purchased_at", "")
                order.setdefault("outcome", "success" if success else
                                 ("cancelled" if "ยกเลิก" in status else
                                  "expired" if "หมดเวลา" in status else
                                  "completed" if "เสร็จสิ้น" in status else "active"))
                if "ได้รับ OTP" in status and self.cloud and not order.get("cloud_recorded"):
                    pending_success.append(str(aid))
                order["remaining"] = max(0, int(order.get("remaining", 0)) - (elapsed if order["active"] else 0))
                if order["active"] and order["remaining"] == 0:
                    order.update(active=False, in_working=False, actionable=False, history=True,
                                 status="หมดเวลา", outcome="expired")
                self.orders[str(aid)] = order
                self.table.insert("", "end", iid=str(aid)); self._sync_row(str(aid))
            if self.orders:
                last = next(reversed(self.orders)); self.table.selection_set(last)
            if any(order["active"] for order in self.orders.values()): self._start_timers()
            for aid in pending_success:
                self.after(0, lambda activation_id=aid: self._record_cloud_success(activation_id))
            self._save_orders()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return

    def _switch_user_orders(self, username):
        self._save_orders(immediate=True)
        for job_name in ("poll_job", "timer_job"):
            job = getattr(self, job_name, None)
            if job:
                try: self.after_cancel(job)
                except tk.TclError: pass
                setattr(self, job_name, None)
        self.polling_ids.clear(); self.cloud_success_pending.clear(); self.orders.clear()
        for item in self.table.get_children(): self.table.delete(item)
        safe_username = "".join(ch for ch in username.lower() if ch.isalnum() or ch in "_.-") or "user"
        os.makedirs(self.data_dir, exist_ok=True)
        self.orders_file = os.path.join(self.data_dir, f"orders-{safe_username}.json")
        legacy_file = os.path.join(self.data_dir, "orders.json")
        if username.lower() == "admin" and not os.path.exists(self.orders_file) and os.path.exists(legacy_file):
            try: os.replace(legacy_file, self.orders_file)
            except OSError: pass
        self._load_orders()

    def _close(self):
        self._save_orders(immediate=True)
        for job in (self.poll_job, self.timer_job):
            if job:
                try: self.after_cancel(job)
                except tk.TclError: pass
        self.destroy()


if __name__ == "__main__":
    WebStyleApp().mainloop()
