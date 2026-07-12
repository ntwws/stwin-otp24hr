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
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from datetime import datetime
from updater import UpdateManager


API_URL = "https://hero-sms.com/stubs/handler_api.php"
COUNTRY = 52
SERVICE = "me"
POLL_MS = 5000
FX_URL = "https://api.frankfurter.dev/v2/rate/USD/THB?providers=BOT"
APP_VERSION = "1.0.0"
UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/ntwws/stwin-otp24hr/main/update.json"


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


class WebStyleApp(tk.Tk):
    """Light, web-style UI with support for up to five simultaneous numbers."""

    def __init__(self):
        super().__init__()
        self.title("OTP24HR by STWIN")
        try:
            self.iconbitmap(resource_path("1.ico"))
        except tk.TclError:
            pass
        width, height = 780, 720
        self.geometry(f"{width}x{height}+{max(0, (self.winfo_screenwidth()-width)//2)}+{max(0, (self.winfo_screenheight()-height)//2)}")
        self.resizable(False, False)
        self.configure(bg="#070510")
        self.jobs = queue.Queue()
        self.quote = self.fx_rate = None
        self.offer_rows = []
        self.orders = {}
        self.polling_ids = set()
        self.cloud_success_pending = set()
        self.credential_store = CredentialStore()
        self.saved_settings = self.credential_store.load()
        self.cloud = CloudClient(CLOUD_API_URL) if CLOUD_API_URL else None
        self.cloud_user = None
        self.cloud_role = None
        self.monthly_success = 0
        self.monthly_purchased = 0
        self.update_manager = UpdateManager(APP_VERSION, UPDATE_MANIFEST_URL)
        self.update_checked = False
        self.poll_job = self.timer_job = None
        self.data_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "HeroLineTH")
        self.orders_file = None
        self._build_ui()
        self.after(100, self._drain_jobs)
        if self.cloud:
            self.after(150, self._require_login)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TEntry", fieldbackground="#120a24", foreground="#f5f3ff", insertcolor="#ffffff",
                        padding=8, bordercolor="#7c3aed")
        style.configure("TButton", font=("Segoe UI", 10, "bold"), padding=(13, 9),
                        background="#20143d", foreground="#f5f3ff", bordercolor="#5b21b6")
        style.map("TButton", background=[("active", "#382164"), ("disabled", "#171126")])
        style.configure("Green.TButton", background="#7c3aed", foreground="#ffffff", bordercolor="#c084fc")
        style.map("Green.TButton", background=[("active", "#9333ea"), ("disabled", "#2e1a47")])
        style.configure("Danger.TButton", background="#c33d3d", foreground="#ffffff", bordercolor="#c33d3d")
        style.configure("Treeview", background="#110a22", fieldbackground="#110a22", foreground="#eee8ff",
                        rowheight=32, bordercolor="#5b21b6", font=("Segoe UI", 10))
        style.configure("Treeview.Heading", background="#2e1065", foreground="#f5f3ff",
                        font=("Segoe UI", 9, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", "#6d28d9")], foreground=[("selected", "#ffffff")])

        card = tk.Frame(self, bg="#100b20", padx=24, pady=14, highlightthickness=1,
                        highlightbackground="#6d28d9")
        card.pack(fill="both", expand=True, padx=24, pady=14)
        tk.Label(card, text="OTP24HR", bg="#ffffff", fg="#13231a",
                 font=("Segoe UI", 21, "bold")).pack(anchor="w")
        tk.Label(card, text="บริการ OTP ประเทศไทย • by STWIN", bg="#ffffff", fg="#617067",
                 font=("Segoe UI", 10)).pack(anchor="w", pady=(2, 18))

        self.key_var = tk.StringVar(value=str(self.saved_settings.get("api_key", "")))

        stats = tk.Frame(card, bg="#ffffff")
        stats.pack(fill="x", pady=18)
        self.price_var = tk.StringVar(value="—")
        self.balance_var = tk.StringVar(value="—")
        for col, (label, value) in enumerate((("บริการ", "LINE"), ("ประเทศ", "ไทย 🇹🇭"),
                                               ("ราคา / คงเหลือ", self.price_var))):
            box = tk.Frame(stats, bg="#f3f8f4", padx=14, pady=12)
            box.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 5, 0 if col == 2 else 5))
            tk.Label(box, text=label, bg="#f3f8f4", fg="#6d7c72", font=("Segoe UI", 9)).pack(anchor="w")
            if isinstance(value, tk.StringVar):
                tk.Label(box, textvariable=value, bg="#f3f8f4", fg="#13231a",
                         font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(3, 0))
            else:
                tk.Label(box, text=value, bg="#f3f8f4", fg="#13231a",
                         font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(3, 0))
        for i in range(3): stats.grid_columnconfigure(i, weight=1, uniform="stats")
        tk.Label(card, textvariable=self.balance_var, bg="#ffffff", fg="#617067",
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 10))

        actions = tk.Frame(card, bg="#ffffff")
        actions.pack(fill="x")
        ttk.Button(actions, text="อัปเดตราคา", command=self.refresh).pack(side="left")
        tk.Label(actions, text="จำนวน", bg="#ffffff", fg="#53675a", font=("Segoe UI", 10)).pack(side="left", padx=(18, 6))
        self.qty_var = tk.IntVar(value=1)
        ttk.Spinbox(actions, from_=1, to=5, textvariable=self.qty_var, width=4, justify="center",
                    state="readonly", font=("Segoe UI", 10)).pack(side="left")
        self.buy_btn = ttk.Button(actions, text="ซื้อหมายเลข", command=self.buy, state="disabled", style="Green.TButton")
        self.buy_btn.pack(side="left", padx=10)
        management = tk.Frame(card, bg="#ffffff")
        management.pack(fill="x", pady=(8, 0))
        self.user_badge = tk.Label(management, text="ผู้ใช้: —", bg="#ffffff", fg="#8fa9c2",
                                   font=("Segoe UI", 9, "bold"))
        self.user_badge.pack(side="left")
        self.admin_report_btn = ttk.Button(management, text="รายงานผู้ใช้", command=self._show_admin_report)
        self.create_user_btn = ttk.Button(management, text="จัดการผู้ใช้", command=self._show_create_user)
        self.topup_btn = ttk.Button(management, text="เติมเงิน", command=self._topup)
        self.settings_btn = ttk.Button(management, text="ตั้งค่า", command=self._show_settings)
        self.settings_btn.pack(side="right")
        self.update_btn = ttk.Button(management, text="อัปเดต", command=lambda: self._check_for_updates(False))
        self.update_btn.pack(side="right", padx=(0, 7))
        self.logout_btn = ttk.Button(management, text="ออกจากระบบ", command=self._logout)
        self.logout_btn.pack(side="right", padx=(0, 7))

        self.notice_var = tk.StringVar(value="กรุณาเข้าสู่ระบบ")
        self.notice = tk.Label(card, textvariable=self.notice_var, bg="#ffffff", fg="#3b4b40",
                               font=("Segoe UI", 10), anchor="w", wraplength=650)
        self.notice.pack(fill="x", pady=(8, 6))

        tk.Frame(card, bg="#294866", height=1).pack(fill="x", pady=(0, 7))
        tk.Label(card, text="รายการหมายเลข / OTP", bg="#ffffff", fg="#13231a",
                 font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 7))
        self.table = ttk.Treeview(card, columns=("number", "cost", "time", "status", "code"),
                                  show="headings", height=5, selectmode="browse")
        for col, title, width in (("number", "หมายเลข", 160), ("cost", "ราคา", 90),
                                  ("time", "เวลา", 65), ("status", "สถานะ", 150), ("code", "OTP", 115)):
            self.table.heading(col, text=title); self.table.column(col, width=width, anchor="center")
        self.table.pack(fill="x")
        self.table.bind("<Button-3>", self._show_order_menu)
        order_actions = tk.Frame(card, bg="#ffffff")
        order_actions.pack(fill="x", pady=(10, 0))
        ttk.Button(order_actions, text="ตรวจ OTP ตอนนี้", command=self.poll_selected).pack(side="left")
        ttk.Button(order_actions, text="เสร็จสิ้น", command=lambda: self.command_selected("complete")).pack(side="left", padx=7)
        ttk.Button(order_actions, text="ยกเลิก", command=lambda: self.command_selected("cancel"),
                   style="Danger.TButton").pack(side="left")
        self._apply_dark_palette(card)

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
            except (tk.TclError, AttributeError): pass
            try:
                fg = widget.cget("foreground")
                if fg in foregrounds: widget.configure(foreground=foregrounds[fg])
            except (tk.TclError, AttributeError): pass
            if widget.winfo_children(): self._apply_dark_palette(widget)

    def _require_login(self):
        saved_username = str(self.saved_settings.get("username", ""))
        saved_password = str(self.saved_settings.get("password", ""))
        if saved_username and saved_password:
            try:
                result = self.cloud.login(saved_username, saved_password)
                self._login_complete(result)
                return
            except Exception:
                pass
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
                messagebox.showerror("เข้าสู่ระบบไม่สำเร็จ", str(exc), parent=self)

    def _login_complete(self, result):
        self.cloud_user = result["username"]
        self.cloud_role = result.get("role", "user")
        self._switch_user_orders(self.cloud_user)
        self.user_badge.configure(text=f"ผู้ใช้: {self.cloud_user} • {self.cloud_role}")
        if self.cloud_role == "admin":
            self.admin_report_btn.pack(side="right", padx=(0, 7))
            self.create_user_btn.pack(side="right", padx=(0, 7))
            self.topup_btn.pack(side="right", padx=(0, 7))
        stats = self.cloud.request("/me/stats")
        self.monthly_purchased = int(stats.get("monthly_purchased", 0))
        self.monthly_success = int(stats.get("monthly_success", 0))
        self.notice_var.set(f"เข้าสู่ระบบ: {self.cloud_user} • ซื้อเดือนนี้ {self.monthly_purchased} • OTP สำเร็จ {self.monthly_success}")
        if not self.update_checked:
            self.update_checked = True
            self.after(1800, lambda: self._check_for_updates(True))

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
        notes = str(manifest.get("notes", "")).strip()
        message = f"พบเวอร์ชันใหม่ v{manifest['version']}\nเวอร์ชันปัจจุบัน v{APP_VERSION}"
        if notes: message += f"\n\n{notes[:220]}"
        if self._themed_confirm("มีอัปเดตใหม่", message + "\n\nดาวน์โหลดตอนนี้หรือไม่?"):
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
        try: report = self.cloud.request("/admin/stats")
        except Exception as exc:
            messagebox.showerror("โหลดรายงานไม่สำเร็จ", str(exc), parent=self); return
        window = tk.Toplevel(self); window.title("รายงาน OTP รายเดือน")
        window.configure(bg="#061321"); window.resizable(False, False)
        try: window.iconbitmap(resource_path("1.ico"))
        except tk.TclError: pass
        width, height = 620, 440
        window.geometry(f"{width}x{height}+{self.winfo_x()+(self.winfo_width()-width)//2}+{self.winfo_y()+(self.winfo_height()-height)//2}")
        panel = tk.Frame(window, bg="#0b1f36", padx=22, pady=18, highlightthickness=1, highlightbackground="#294866")
        panel.pack(fill="both", expand=True, padx=16, pady=16)
        tk.Label(panel, text="รายงานผู้ใช้", bg="#0b1f36", fg="#eaf6ff", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        tk.Label(panel, text=f"สรุปประจำเดือน {report.get('month','—')} • นับเฉพาะรายการที่ได้รับ OTP สำเร็จ",
                 bg="#0b1f36", fg="#8fa9c2", font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 13))
        table = ttk.Treeview(panel, columns=("user", "purchased", "success", "last"), show="headings", height=10)
        for column, title, size in (("user", "Username", 150), ("purchased", "ซื้อทั้งหมด", 100),
                                    ("success", "ได้รับ OTP", 100), ("last", "สำเร็จล่าสุด", 170)):
            table.heading(column, text=title); table.column(column, width=size, anchor="center")
        total = 0
        for row in report.get("users", []):
            success = int(row.get("monthly_success", 0)); total += success
            table.insert("", "end", values=(row.get("username", "—"), row.get("monthly_purchased", 0),
                                             success, row.get("last_success") or "—"))
        table.pack(fill="both", expand=True)
        footer = tk.Frame(panel, bg="#0b1f36"); footer.pack(fill="x", pady=(12, 0))
        tk.Label(footer, text=f"รวม OTP สำเร็จเดือนนี้: {total:,} เบอร์", bg="#0b1f36", fg="#58d6ff",
                 font=("Segoe UI", 11, "bold")).pack(side="left")
        ttk.Button(footer, text="ปิด", command=window.destroy).pack(side="right")
        self._apply_dark_palette(window)

    def _show_create_user(self):
        if self.cloud_role != "admin": return
        window = tk.Toplevel(self); window.title("สร้างบัญชีผู้ใช้")
        window.configure(bg="#061321"); window.resizable(False, False); window.transient(self); window.grab_set()
        try: window.iconbitmap(resource_path("1.ico"))
        except tk.TclError: pass
        width, height = 450, 570
        window.geometry(f"{width}x{height}+{self.winfo_x()+(self.winfo_width()-width)//2}+{self.winfo_y()+(self.winfo_height()-height)//2}")
        panel = tk.Frame(window, bg="#0b1f36", padx=27, pady=23, highlightthickness=1, highlightbackground="#294866")
        panel.pack(fill="both", expand=True, padx=16, pady=16)
        buttons = tk.Frame(panel, bg="#0b1f36", height=42)
        buttons.pack(fill="x", side="bottom", pady=(12, 0))
        buttons.pack_propagate(False)
        ttk.Button(buttons, text="ปิด", command=window.destroy).pack(side="right", fill="y")
        tk.Label(panel, text="สร้างบัญชีผู้ใช้", bg="#0b1f36", fg="#eaf6ff",
                 font=("Segoe UI", 18, "bold")).pack(anchor="w")
        tk.Label(panel, text="บัญชีใหม่จะสามารถใช้งานโปรแกรมและซื้อเบอร์จากกระเป๋าร่วมได้",
                 bg="#0b1f36", fg="#8fa9c2", font=("Segoe UI", 9), wraplength=365,
                 justify="left").pack(anchor="w", pady=(2, 17))
        username_var, password_var, confirm_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        entries = []
        for label, variable, hidden in (("Username", username_var, False), ("Password", password_var, True),
                                        ("ยืนยัน Password", confirm_var, True)):
            tk.Label(panel, text=label, bg="#0b1f36", fg="#b8cbdd", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(5, 4))
            entry = ttk.Entry(panel, textvariable=variable, show="•" if hidden else "", font=("Segoe UI", 10))
            entry.pack(fill="x", ipady=4); entries.append(entry)
        tk.Label(panel, text="Role", bg="#0b1f36", fg="#b8cbdd", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(9, 4))
        role_var = tk.StringVar(value="user")
        role_box = ttk.Combobox(panel, textvariable=role_var, values=("user", "admin"), state="readonly",
                                font=("Segoe UI", 10))
        role_box.pack(fill="x", ipady=3)
        status = tk.Label(panel, text="", bg="#0b1f36", fg="#ff7b7b", font=("Segoe UI", 9), anchor="w")
        status.pack(fill="x", pady=(8, 0))
        def create():
            username, password = username_var.get().strip(), password_var.get()
            if len(username) < 3:
                status.configure(text="Username ต้องมีอย่างน้อย 3 ตัวอักษร", fg="#ff7b7b"); return
            if len(password) < 8:
                status.configure(text="Password ต้องมีอย่างน้อย 8 ตัวอักษร", fg="#ff7b7b"); return
            if password != confirm_var.get():
                status.configure(text="ยืนยัน Password ไม่ตรงกัน", fg="#ff7b7b"); return
            role = role_var.get()
            try: self.cloud.request("/admin/users", "POST", {"username": username, "password": password, "role": role})
            except Exception as exc:
                status.configure(text=str(exc), fg="#ff7b7b"); return
            username_var.set(""); password_var.set(""); confirm_var.set("")
            status.configure(text=f"สร้างบัญชี {username} ({role}) สำเร็จแล้ว", fg="#58d6ff")
            messagebox.showinfo("สร้างบัญชีสำเร็จ", f"สร้างบัญชี {username}\nRole: {role}", parent=window)
        ttk.Button(buttons, text="สร้างบัญชี", command=create, style="Green.TButton").pack(side="right", fill="y", padx=(0, 8))
        entries[0].focus_set()
        self._apply_dark_palette(window)

    def _topup(self):
        if self.cloud_role != "admin": return
        if messagebox.askyesno("เติมเงิน", "ระบบจะเปิดหน้าชำระเงินภายนอกในเว็บเบราว์เซอร์\nต้องการดำเนินการต่อหรือไม่?", parent=self):
            webbrowser.open("https://hero-sms.com/", new=2)

    def _logout(self):
        if any(order.get("active") for order in self.orders.values()):
            messagebox.showwarning("ยังออกจากระบบไม่ได้", "กรุณาจัดการรายการที่กำลังรอ OTP ให้เสร็จก่อน", parent=self); return
        if not messagebox.askyesno("ออกจากระบบ", "ต้องการออกจากระบบและลบข้อมูลเข้าสู่ระบบที่จดจำไว้หรือไม่?", parent=self): return
        self.saved_settings.pop("username", None); self.saved_settings.pop("password", None)
        try: self.credential_store.save(self.saved_settings)
        except OSError: pass
        self.cloud = CloudClient(CLOUD_API_URL)
        self.cloud_user = self.cloud_role = None
        self.user_badge.configure(text="ผู้ใช้: —")
        self.monthly_purchased = self.monthly_success = 0
        self.admin_report_btn.pack_forget(); self.create_user_btn.pack_forget(); self.topup_btn.pack_forget()
        for item in self.table.get_children(): self.table.delete(item)
        self.orders.clear()
        self.notice_var.set("ออกจากระบบแล้ว")
        self.after(100, self._require_login)

    def _show_settings(self):
        if not self.cloud_user: return
        try: stats = self.cloud.request("/me/stats")
        except Exception as exc:
            messagebox.showerror("โหลดข้อมูลไม่สำเร็จ", str(exc), parent=self); return
        self.monthly_purchased = int(stats.get("monthly_purchased", 0))
        self.monthly_success = int(stats.get("monthly_success", 0))
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
        dialog.geometry(f"{width}x{height}+{self.winfo_x() + (self.winfo_width()-width)//2}+{self.winfo_y() + (self.winfo_height()-height)//2}")
        dialog.transient(self); dialog.grab_set()
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
        username_entry.focus_set()
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
        try:
            while True:
                fn, args = self.jobs.get_nowait(); fn(*args)
        except queue.Empty: pass
        self.after(100, self._drain_jobs)

    def _error(self, message):
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
        elif rate is None: self.price_var.set(f"${self.quote:.4f} / {stock:,}")
        else: self.price_var.set(f"${self.quote:.4f} ≈ ฿{self.quote*rate:.2f} / {stock:,}")
        thb = "—" if rate is None else f"฿{balance*rate:,.2f}"
        cloud_stats = f" • OTP เดือนนี้ {self.monthly_success} เบอร์" if self.cloud_user else ""
        self.balance_var.set(f"ยอดคงเหลือ ${balance:.4f} USD (≈ {thb} THB) • เรต {rate_date or '—'}{cloud_stats}")
        self.notice_var.set(f"พร้อมใช้งาน • อัปเดต {datetime.now().strftime('%H:%M:%S')}")
        self.buy_btn.configure(state="normal" if self.quote is not None and stock else "disabled")

    def buy(self):
        if self.quote is None: return
        qty = max(1, min(5, int(self.qty_var.get())))
        if self.cloud:
            try:
                self.cloud.request("/queue/acquire", "POST", {})
            except Exception as exc:
                messagebox.showwarning("มีผู้ใช้อื่นกำลังซื้อ", str(exc), parent=self); return
        if not messagebox.askyesno("ยืนยันการซื้อ", f"ซื้อหมายเลข LINE ประเทศไทย {qty} เบอร์\nราคาเริ่มต้นเบอร์ละ ${self.quote:.4f} ?"):
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
        for item in bought:
            aid, phone, price = item["aid"], item["phone"], item["price"]
            status = "กำลังรอ SMS…" if not item["ready_error"] else "ซื้อแล้ว • รอตรวจสถานะ"
            self.orders[aid] = {"phone": "+" + phone.lstrip("+"), "price": price,
                                "remaining": 1200, "status": status, "code": "—", "active": True,
                                "cloud_recorded": False}
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
        if aid: self._poll_ids([aid])

    def _poll_ids(self, ids):
        ids = [aid for aid in ids if aid in self.orders and aid not in self.polling_ids]
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
                self.orders[aid]["status"] = f"ตรวจไม่สำเร็จ: {error}"
                self._sync_row(aid)
                continue
            state, _, value = str(raw).partition(":")
            if state == "STATUS_OK":
                self.orders[aid].update(status="ได้รับ OTP แล้ว", code=value, active=False)
                if self.cloud and not self.orders[aid].get("cloud_recorded"):
                    self._record_cloud_success(aid)
            else:
                self.orders[aid]["status"] = ERRORS.get(state, str(raw))
                if state == "STATUS_CANCEL":
                    self.orders.pop(aid, None)
                    if self.table.exists(aid): self.table.delete(aid)
                    continue
            self._sync_row(aid)
        self._save_orders()

    def _record_cloud_success(self, aid):
        if aid in self.cloud_success_pending or aid not in self.orders: return
        self.cloud_success_pending.add(aid)
        def work():
            try:
                self.cloud.request("/activations/success", "POST", {"activation_id": aid})
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
            self._save_orders()

    def command_selected(self, name):
        aid = self._selected_id()
        if not aid: return
        if name == "cancel" and not self._themed_confirm("ยืนยันการยกเลิก", "ยืนยันยกเลิกหมายเลขที่เลือก?"): return
        status = 6 if name == "complete" else 8
        self._run(lambda: self._client().request("setStatus", id=aid, status=status),
                  lambda _: self._commanded(aid, name))

    def _commanded(self, aid, name):
        if aid in self.orders:
            if name == "cancel":
                self.orders.pop(aid, None)
                if self.table.exists(aid): self.table.delete(aid)
            else:
                self.orders[aid]["status"] = "เสร็จสิ้นแล้ว"
                self.orders[aid]["active"] = False; self.polling_ids.discard(aid); self._sync_row(aid)
            self._save_orders()

    def _sync_row(self, aid):
        order = self.orders[aid]; minutes, seconds = divmod(max(0, order["remaining"]), 60)
        self.table.item(aid, values=(order["phone"], f"${order['price']:.4f}",
                                    f"{minutes:02d}:{seconds:02d}", order["status"], order["code"]))

    def _start_timers(self):
        if self.timer_job is None: self.timer_job = self.after(1000, self._tick)
        if self.poll_job is None: self.poll_job = self.after(POLL_MS, self._auto_poll)

    def _tick(self):
        active = False
        for aid, order in self.orders.items():
            if order["active"]:
                order["remaining"] = max(0, order["remaining"] - 1)
                if order["remaining"] == 0:
                    order["active"] = False
                    order["status"] = "หมดเวลา"
                    self.polling_ids.discard(aid)
                    self._save_orders()
                else:
                    active = True
                self._sync_row(aid)
        self.timer_job = self.after(1000, self._tick) if active else None

    def _auto_poll(self):
        ids = [aid for aid, order in self.orders.items() if order["active"]]
        if ids:
            if len(self.key_var.get().strip()) >= 8:
                self._poll_ids(ids)
            self.poll_job = self.after(POLL_MS, self._auto_poll)
        else: self.poll_job = None

    def _show_order_menu(self, event):
        row = self.table.identify_row(event.y)
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
        result = {"value": False}; window = tk.Toplevel(self); window.title(title)
        window.configure(bg="#070510"); window.resizable(False, False); window.transient(self); window.grab_set()
        width, height = 410, 220
        window.geometry(f"{width}x{height}+{self.winfo_x()+(self.winfo_width()-width)//2}+{self.winfo_y()+(self.winfo_height()-height)//2}")
        panel = tk.Frame(window, bg="#100b20", padx=25, pady=22, highlightthickness=1, highlightbackground="#7c3aed")
        panel.pack(fill="both", expand=True, padx=14, pady=14)
        tk.Label(panel, text=title, bg="#100b20", fg="#f5f3ff", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(panel, text=message, bg="#100b20", fg="#cfc3e6", font=("Segoe UI", 10), justify="left").pack(anchor="w", pady=(9, 18))
        buttons = tk.Frame(panel, bg="#100b20"); buttons.pack(fill="x", side="bottom")
        def finish(value): result["value"] = value; window.destroy()
        ttk.Button(buttons, text="ยกเลิก", command=lambda: finish(False)).pack(side="right")
        ttk.Button(buttons, text="ยืนยัน", command=lambda: finish(True), style="Green.TButton").pack(side="right", padx=(0, 8))
        self.wait_window(window); return result["value"]

    def _themed_days_dialog(self):
        result = {"value": None}; window = tk.Toplevel(self); window.title("กำหนดจำนวนวัน")
        window.configure(bg="#070510"); window.resizable(False, False); window.transient(self); window.grab_set()
        width, height = 400, 240
        window.geometry(f"{width}x{height}+{self.winfo_x()+(self.winfo_width()-width)//2}+{self.winfo_y()+(self.winfo_height()-height)//2}")
        panel = tk.Frame(window, bg="#100b20", padx=25, pady=22, highlightthickness=1, highlightbackground="#7c3aed")
        panel.pack(fill="both", expand=True, padx=14, pady=14)
        tk.Label(panel, text="รายงานติดลิมิต", bg="#100b20", fg="#f5f3ff", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(panel, text="ติดลิมิตกี่วัน? (1–365)", bg="#100b20", fg="#cfc3e6", font=("Segoe UI", 9)).pack(anchor="w", pady=(8, 5))
        value = tk.StringVar(); entry = ttk.Entry(panel, textvariable=value, font=("Segoe UI", 11)); entry.pack(fill="x", ipady=4)
        status = tk.Label(panel, text="", bg="#100b20", fg="#ff7b7b", font=("Segoe UI", 9)); status.pack(anchor="w")
        buttons = tk.Frame(panel, bg="#100b20"); buttons.pack(fill="x", side="bottom")
        def submit():
            try: days = int(value.get())
            except ValueError: days = 0
            if not 1 <= days <= 365: status.configure(text="กรุณากรอกตัวเลข 1–365"); return
            result["value"] = days; window.destroy()
        ttk.Button(buttons, text="ยกเลิก", command=window.destroy).pack(side="right")
        ttk.Button(buttons, text="ตกลง", command=submit, style="Green.TButton").pack(side="right", padx=(0, 8))
        entry.bind("<Return>", lambda _e: submit()); entry.focus_set(); self.wait_window(window)
        return result["value"]

    def _save_orders(self):
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
            for aid, order in loaded.items():
                if not isinstance(order, dict) or not order.get("phone"):
                    continue
                order.setdefault("price", 0.0); order.setdefault("code", "—")
                order.setdefault("cloud_recorded", False)
                order.setdefault("status", "ไม่ทราบสถานะ"); order.setdefault("active", False)
                order["remaining"] = max(0, int(order.get("remaining", 0)) - (elapsed if order["active"] else 0))
                if order["active"] and order["remaining"] == 0:
                    order["active"] = False; order["status"] = "หมดเวลา"
                self.orders[str(aid)] = order
                self.table.insert("", "end", iid=str(aid)); self._sync_row(str(aid))
            if self.orders:
                last = next(reversed(self.orders)); self.table.selection_set(last)
            if any(order["active"] for order in self.orders.values()): self._start_timers()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return

    def _switch_user_orders(self, username):
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
        self._save_orders()
        for job in (self.poll_job, self.timer_job):
            if job:
                try: self.after_cancel(job)
                except tk.TclError: pass
        self.destroy()


if __name__ == "__main__":
    WebStyleApp().mainloop()
