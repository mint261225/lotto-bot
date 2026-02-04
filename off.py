# -*- coding: utf-8 -*-
import os
import re
import time
import json
from io import BytesIO
from typing import Any, Dict, Iterable, List, Optional, Tuple
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from flask import Flask, request, abort, Response
from dotenv import load_dotenv
from linebot.v3.webhook import WebhookHandler
from linebot.v3.webhooks import MessageEvent
from linebot.v3.webhooks.models import TextMessageContent
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    MulticastRequest,
    TextMessage,
    ImageMessage,
)
from linebot.v3.messaging.exceptions import ApiException
load_dotenv()
CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN", "").strip()
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip()
if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("Missing CHANNEL_ACCESS_TOKEN or CHANNEL_SECRET in environment/.env")
# Paths / storage
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)
TARGETS_PATH = os.path.join(BASE_DIR, "targets.json")
LOTTO_IMAGE_FILENAME = "lotto_latest.png"
LOTTO_IMAGE_PATH = f"/static/{LOTTO_IMAGE_FILENAME}"
# Flask + LINE
app = Flask(__name__)
handler = WebhookHandler(CHANNEL_SECRET)
config = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
ZERO_REPLY_TOKEN = "00000000000000000000000000000000"
# Utilities
def _is_https(url: str) -> bool:
    return isinstance(url, str) and url.lower().startswith("https://")
def _load_targets() -> Dict[str, Any]:
    """อ่าน targets.json (เก็บเฉพาะ 'groups' เท่านั้น ไม่เก็บ 'rooms')"""
    if not os.path.exists(TARGETS_PATH):
        return {"settings": {"remember_enabled": False}, "groups": {}}
    try:
        with open(TARGETS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        data.setdefault("settings", {"remember_enabled": False})
        data.setdefault("groups", {})
        # ล้างข้อมูล rooms เก่า (ถ้ามี) เพื่อไม่ให้เก็บ/ส่งต่อ
        data.pop("rooms", None)
        return data
    except Exception:
        return {"settings": {"remember_enabled": False}, "groups": {}}

def _save_targets(data: Dict[str, Any]) -> None:
    # บังคับไม่เขียน key 'rooms' ลงไฟล์ (เผื่อมีหลงมา)
    data.pop("rooms", None)
    try:
        with open(TARGETS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        app.logger.warning(f"save targets.json failed: {e}")

def remember_enabled() -> bool:
    data = _load_targets()
    return bool((data.get("settings") or {}).get("remember_enabled", False))
def set_remember_enabled(enabled: bool) -> None:
    data = _load_targets()
    data.setdefault("settings", {})
    data["settings"]["remember_enabled"] = bool(enabled)
    _save_targets(data)
def current_target_id(event: MessageEvent) -> Optional[str]:
    src = event.source
    # group/room/user (room จะไม่ถูกบันทึก)
    return getattr(src, "group_id", None) or getattr(src, "room_id", None) or getattr(src, "user_id", None)
def remember_target(event: MessageEvent):
    """เวอร์ชัน Debug: เก็บเฉพาะ Group ID (ไม่เก็บ Room)"""
    print(f"--- 🔍 เริ่มตรวจสอบ: มีข้อความเข้า ---")

    # เช็กว่าเปิดโหมดจำหรือไม่
    is_enabled = remember_enabled()
    print(f"   สถานะโหมดจำ: {'เปิด ✅' if is_enabled else 'ปิด ⛔'}")
    if not is_enabled:
        print("   ❌ จบการทำงาน: เพราะโหมดจำปิดอยู่")
        return

    src = event.source
    gid = getattr(src, "group_id", None)
    print(f"   ชนิด Source: {src.type}")
    print(f"   Group ID: {gid}")

    # เก็บเฉพาะกลุ่มเท่านั้น
    if not gid:
        print("   ❌ จบการทำงาน: ไม่ใช่ Group (อาจเป็น Room หรือแชทส่วนตัว)")
        return

    # ดึงชื่อกลุ่ม
    name = None
    try:
        with ApiClient(config) as api_client:
            api = MessagingApi(api_client)
            print("   ...กำลังขอชื่อกลุ่มจาก LINE API...")
            prof = api.get_group_summary(gid)
            name = getattr(prof, "group_name", None)
            print(f"   ✅ ได้ชื่อกลุ่มมาว่า: {name}")
    except Exception as e:
        print(f"   ⚠️ ดึงชื่อกลุ่มไม่ได้ (Error: {e}) -> จะใช้ชื่อเดิมหรือเว้นว่าง")
        name = None

    # บันทึกลงไฟล์ (groups เท่านั้น)
    try:
        data = _load_targets()
        data.setdefault("groups", {})
        cur = data["groups"].get(gid) or {}

        # อัปเดตชื่อ
        if name:
            cur["name"] = name
        else:
            cur.setdefault("name", cur.get("name") or "(ไม่ทราบชื่อกลุ่ม)")

        cur["updated_at"] = int(time.time())
        data["groups"][gid] = cur

        _save_targets(data)
        print(f"   ✅✅ SAVE SUCCESS! บันทึก Group ID ลง targets.json เรียบร้อย")
    except Exception as e:
        print(f"   🔥 SAVE FAILED: เกิดข้อผิดพลาดตอนบันทึกไฟล์: {e}")

def iter_all_targets(exclude_id: Optional[str] = None) -> Iterable[str]:
    data = _load_targets()
    for gid in (data.get("groups") or {}).keys():
        if gid and gid != exclude_id:
            yield gid

def build_customers_text() -> str:
    data = _load_targets()
    enabled = bool((data.get("settings") or {}).get("remember_enabled", False))
    groups: Dict[str, Any] = data.get("groups", {}) or {}
    total = len(groups)
    status = "เปิด✅" if enabled else "ปิด⛔"

    lines: List[str] = []
    lines.append(f"โหมดจำชื่อกลุ่ม/ID: {status}")
    lines.append("")
    lines.append("📒 รายชื่อลูกค้าที่บันทึกไว้ ")
    lines.append(f"รวมทั้งหมด: {total} (กลุ่ม {len(groups)})")

    # Groups
    lines.append("👥 กลุ่ม (Group)")
    if groups:
        def gkey(item):
            _, g = item
            name = (g or {}).get("name") or ""
            return name.lower()

        for i, (_, g) in enumerate(sorted(groups.items(), key=gkey), start=1):
            name = (g or {}).get("name") or "(ไม่ทราบชื่อกลุ่ม)"
            lines.append(f" {i}: {name}")
    else:
        lines.append(" (ยังไม่มี)")

    lines.append("")
    lines.append("รายการคำสั่ง:")
    lines.append("- /ลูกค้า          (ดูรายชื่อกลุ่มที่บันทึกไว้)")
    lines.append("- /ลูกค้า เปิด/ปิด   (เริ่ม/หยุดจำชื่อกลุ่ม)")
    lines.append("- /ปิดรับ          (ส่งรูปปิดรับให้ทุกกลุ่ม)")
    lines.append("- /ผลหวย           (สร้างรูปผลหวยและบันทึก)")
    lines.append("- /ส่งผลหวย ลิ้ง    (ส่งรูปแบบลิ้งให้ทุกกลุ่ม)")
    lines.append("- /แจ้งโอน         (ส่งรูปแจ้งโอนให้ทุกกลุ่ม)")
    return "\n".join(lines).rstrip()

def reply_messages(reply_token: str, messages: List[Any]) -> None:
    with ApiClient(config) as api_client:
        api = MessagingApi(api_client)
        req = ReplyMessageRequest(reply_token=reply_token, messages=messages)
        api.reply_message(req)
def push_messages(to: str, messages: List[Any]) -> None:
    with ApiClient(config) as api_client:
        api = MessagingApi(api_client)
        req = PushMessageRequest(to=to, messages=messages)
        api.push_message(req)
def safe_send(event: MessageEvent, messages: List[Any]) -> None:
    """reply ก่อน ถ้า reply token ใช้ไม่ได้ค่อย fallback เป็น push"""
    to_id = current_target_id(event)
    reply_token = getattr(event, "reply_token", None)
    # invalid token or missing -> push
    if (not reply_token) or (reply_token == ZERO_REPLY_TOKEN):
        if to_id:
            try:
                push_messages(to_id, messages)
            except Exception as e:
                app.logger.warning(f"push failed (no reply token) to {to_id}: {e}")
        return
    try:
        reply_messages(reply_token, messages)
    except ApiException as e:
        body = getattr(e, "body", "") or ""
        if isinstance(body, (bytes, bytearray)):
            try:
                body = body.decode("utf-8", errors="ignore")
            except Exception:
                body = str(body)
        if "Invalid reply token" in str(body):
            if to_id:
                try:
                    push_messages(to_id, messages)
                    return
                except Exception as e2:
                    app.logger.warning(f"push fallback failed to {to_id}: {e2}")
            return
        app.logger.warning(f"reply failed: {e} body={body}")
    except Exception as e:
        app.logger.warning(f"reply failed (unknown): {e}")
def push_to_all(messages: List[Any], exclude_id: Optional[str] = None) -> None:
    # 1. รวบรวม ID กลุ่มทั้งหมดมาก่อน (ตัดกลุ่มที่คนพิมพ์สั่งออก ถ้ามี)
    all_targets = list(iter_all_targets(exclude_id=exclude_id))
    if not all_targets:
        print("⚠️ ไม่มีกลุ่มเป้าหมายให้ส่ง")
        return
    print(f"กำลังเริ่มส่ง Multicast ไปยัง {len(all_targets)} กลุ่ม...")
    # 2. LINE Multicast ส่งได้สูงสุดทีละ 500 ID
    chunk_size = 500
    with ApiClient(config) as api_client:
        api = MessagingApi(api_client)
        # วนลูปส่งทีละก้อน (สำหรับคุณที่มี 300 กลุ่ม จะทำงานรอบเดียวจบ)
        for i in range(0, len(all_targets), chunk_size):
            chunk = all_targets[i : i + chunk_size]
            try:
                req = MulticastRequest(to=chunk, messages=messages)
                api.multicast(req)
                print(f"✅ ส่งสำเร็จไปแล้ว {len(chunk)} กลุ่ม")
            except ApiException as e:
                app.logger.warning(f"Multicast failed: {e}")
                print(f"❌ ส่งไม่ผ่าน: {e}")
            except Exception as e:
                app.logger.warning(f"Multicast error: {e}")
# Fonts
FONT_REGULAR_PATH = os.path.join(BASE_DIR, "fonts", "Sarabun-Regular.ttf")
FONT_BOLD_PATH = os.path.join(BASE_DIR, "fonts", "Sarabun-Bold.ttf")
def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD_PATH if bold else FONT_REGULAR_PATH
    try:
        return ImageFont.truetype(path, size)
    except:
        return ImageFont.load_default()
# Lotto fetching
LOTTERY_CO_TH_URL = "https://www.lottery.co.th/"
SANOOK_ICHECK_URL = "https://news.sanook.com/lotto/icheck/"
THAI_MONTHS_ABBR = {
    "ม.ค.": "มกราคม", "ก.พ.": "กุมภาพันธ์", "มี.ค.": "มีนาคม", "เม.ย.": "เมษายน",
    "พ.ค.": "พฤษภาคม", "มิ.ย.": "มิถุนายน", "ก.ค.": "กรกฎาคม", "ส.ค.": "สิงหาคม",
    "ก.ย.": "กันยายน", "ต.ค.": "ตุลาคม", "พ.ย.": "พฤศจิกายน", "ธ.ค.": "ธันวาคม",
}
_cache = {"ts": 0.0, "data": None}
def _normalize_date_th_from_short(short_date: str) -> str:
    s = (short_date or "").strip()
    m = re.search(r"(\d{1,2})\s+([ก-๙]{1,4}\.)\s+(\d{2})", s)
    if not m:
        return s
    day = int(m.group(1))
    mon_abbr = m.group(2)
    yy = int(m.group(3))
    mon_full = THAI_MONTHS_ABBR.get(mon_abbr, mon_abbr)
    be_year = 2500 + yy
    return f"{day} {mon_full} {be_year}"
def fetch_lotto_from_lottery_co_th() -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(LOTTERY_CO_TH_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        txt = soup.get_text("\n", strip=True)
        pat = re.compile(
            r"(\d{1,2}\s+[ก-๙]{1,4}\.\s+\d{2}).{0,120}?"
            r"(\d{6})\s+(\d{2})\s+(\d{3})\s+(\d{3})\s+(\d{3})\s+(\d{3})"
        )
        m = pat.search(txt)
        if not m:
            return None
        short_date = m.group(1)
        first = m.group(2)
        last2 = m.group(3)
        last3a, last3b = m.group(4), m.group(5)
        front3a, front3b = m.group(6), m.group(7)
        return {
            "date_th": _normalize_date_th_from_short(short_date),
            "first": first,
            "front3": [front3a, front3b],
            "last3": [last3a, last3b],
            "last2": last2,
        }
    except Exception as e:
        app.logger.warning(f"lottery.co.th parse failed: {e}")
        return None
def fetch_lotto_from_sanook_icheck() -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(SANOOK_ICHECK_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        txt = BeautifulSoup(r.text, "html.parser").get_text("\n", strip=True)
        pattern = re.compile(
            r"(\d{1,2}\s+\S+\s+\d{4}).{0,1200}?"
            r"รางวัลที่ 1\s+(\d{6}).{0,800}?"
            r"เลขหน้า 3 ตัว\s+(\d{3})\s+(\d{3}).{0,800}?"
            r"เลขท้าย 3 ตัว\s+(\d{3})\s+(\d{3}).{0,800}?"
            r"เลขท้าย 2 ตัว\s+(\d{2})",
            re.S
        )
        m = pattern.search(txt)
        if not m:
            return None
        return {
            "date_th": m.group(1),
            "first": m.group(2),
            "front3": [m.group(3), m.group(4)],
            "last3": [m.group(5), m.group(6)],
            "last2": m.group(7),
        }
    except Exception as e:
        app.logger.warning(f"sanook icheck parse failed: {e}")
        return None
def fetch_latest_lotto(force: bool = False) -> Optional[Dict[str, Any]]:
    now_ts = time.time()
    if (not force) and _cache["data"] and (now_ts - _cache["ts"] < 300):
        return _cache["data"]
    for fn, tag in [
        (fetch_lotto_from_lottery_co_th, "lottery.co.th"),
        (fetch_lotto_from_sanook_icheck, "sanook"),
    ]:
        data = fn()
        if data:
            app.logger.info(f"lotto picked {tag}: {data.get('date_th')}")
            _cache["ts"] = now_ts
            _cache["data"] = data
            return data
    _cache["ts"] = now_ts
    _cache["data"] = None
    return None
# Lotto image rendering
def render_lotto_image_clean(data: Dict[str, Any]) -> bytes:
    W, H = 1200, 720
    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    top_color = (245, 247, 250)
    bottom_color = (255, 210, 230)
    for y in range(H):
        t = y / max(H - 1, 1)
        r = int(top_color[0] * (1 - t) + bottom_color[0] * t)
        g = int(top_color[1] * (1 - t) + bottom_color[1] * t)
        b = int(top_color[2] * (1 - t) + bottom_color[2] * t)
        draw.line([(0, y), (W, y)], fill=(r, g, b))
    COLOR_PINK = (233, 30, 99)
    COLOR_PINK_SOFT = (255, 105, 180)
    COLOR_TEXT = (30, 30, 30)
    COLOR_CARD = (255, 255, 255)
    COLOR_BORDER = (240, 180, 210)
    font_title = _load_font(52, bold=True)
    font_date = _load_font(30, bold=True)
    font_label_big = _load_font(32, bold=True)
    font_num_big = _load_font(120, bold=True)
    font_label = _load_font(28, bold=True)
    font_num = _load_font(56, bold=True)
    draw.text((W / 2, 80), "ผลสลากกินแบ่งรัฐบาล", font=font_title, fill=COLOR_PINK, anchor="mm")
    date_th = str(data.get("date_th", "")).strip()
    date_text = f"งวดประจำวันที่  {date_th}" if date_th else "งวดประจำวันที่ -"
    pad_x, pad_y = 40, 12
    bbox = draw.textbbox((0, 0), date_text, font=font_date)
    w_date = bbox[2] - bbox[0]
    h_date = bbox[3] - bbox[1]
    box_w, box_h = w_date + pad_x * 2, h_date + pad_y * 2
    box_x0, box_y0 = (W - box_w) / 2, 130
    box_x1, box_y1 = box_x0 + box_w, box_y0 + box_h
    draw.rounded_rectangle((box_x0, box_y0, box_x1, box_y1), radius=25, fill=COLOR_PINK, outline=None)
    draw.text(((box_x0 + box_x1) / 2, (box_y0 + box_y1) / 2), date_text, font=font_date, fill=(255, 255, 255), anchor="mm")
    margin = 70
    main_card = (margin, 210, int(W * 0.68), 520)
    last2_card = (int(W * 0.72), 210, W - margin, 520)
    front3_card = (margin, 540, int(W * 0.5) - 10, 690)
    last3_card = (int(W * 0.5) + 10, 540, W - margin, 690)
    def card(box, radius=35):
        draw.rounded_rectangle(box, radius=radius, fill=COLOR_CARD, outline=COLOR_BORDER, width=2)
    card(main_card)
    card(last2_card)
    card(front3_card, radius=28)
    card(last3_card, radius=28)
    x0, y0, x1, y1 = main_card
    band_h = 80
    draw.rounded_rectangle((x0, y0, x1, y0 + band_h), radius=35, fill=COLOR_PINK_SOFT, outline=None)
    draw.rectangle((x0, y0 + band_h - 20, x1, y0 + band_h), fill=COLOR_PINK_SOFT)
    draw.text(((x0 + x1) / 2, y0 + band_h / 2 + 2), "รางวัลที่ 1", font=font_label_big, fill=(255, 255, 255), anchor="mm")
    first = str(data.get("first", "")).strip() or "-"
    draw.text(((x0 + x1) / 2, (y0 + y1) / 2 + 30), first, font=font_num_big, fill=COLOR_TEXT, anchor="mm")
    x0, y0, x1, y1 = last2_card
    draw.rounded_rectangle((x0, y0, x1, y0 + band_h), radius=35, fill=COLOR_PINK_SOFT, outline=None)
    draw.rectangle((x0, y0 + band_h - 20, x1, y0 + band_h), fill=COLOR_PINK_SOFT)
    draw.text(((x0 + x1) / 2, y0 + band_h / 2 + 2), "เลขท้าย 2 ตัว", font=font_label, fill=(255, 255, 255), anchor="mm")
    last2 = str(data.get("last2", "")).zfill(2) if data.get("last2") is not None else "-"
    draw.text(((x0 + x1) / 2, (y0 + y1) / 2 + 24), last2, font=font_num_big, fill=COLOR_TEXT, anchor="mm")
    x0, y0, x1, y1 = front3_card
    band_h2 = 60
    draw.rounded_rectangle((x0, y0, x1, y0 + band_h2), radius=28, fill=COLOR_PINK, outline=None)
    draw.rectangle((x0, y0 + band_h2 - 18, x1, y0 + band_h2), fill=COLOR_PINK)
    draw.text(((x0 + x1) / 2, y0 + band_h2 / 2 + 1), "เลขหน้า 3 ตัว", font=font_label, fill=(255, 255, 255), anchor="mm")
    f = data.get("front3") or []
    f1 = str(f[0]).zfill(3) if len(f) > 0 and f[0] else "---"
    f2 = str(f[1]).zfill(3) if len(f) > 1 and f[1] else "---"
    draw.text(((x0 + x1) / 2, (y0 + y1) / 2 + 26), f"{f1}   {f2}", font=font_num, fill=COLOR_TEXT, anchor="mm")
    x0, y0, x1, y1 = last3_card
    draw.rounded_rectangle((x0, y0, x1, y0 + band_h2), radius=28, fill=COLOR_PINK, outline=None)
    draw.rectangle((x0, y0 + band_h2 - 18, x1, y0 + band_h2), fill=COLOR_PINK)
    draw.text(((x0 + x1) / 2, y0 + band_h2 / 2 + 1), "เลขท้าย 3 ตัว", font=font_label, fill=(255, 255, 255), anchor="mm")
    l = data.get("last3") or []
    l1 = str(l[0]).zfill(3) if len(l) > 0 and l[0] else "---"
    l2 = str(l[1]).zfill(3) if len(l) > 1 and l[1] else "---"
    draw.text(((x0 + x1) / 2, (y0 + y1) / 2 + 26), f"{l1}   {l2}", font=font_num, fill=COLOR_TEXT, anchor="mm")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
def save_lotto_image_to_static(data: Dict[str, Any]) -> str:
    """สร้างรูปผลหวยและเซฟลง static คืนค่า filepath"""
    png = render_lotto_image_clean(data)
    file_path = os.path.join(STATIC_DIR, LOTTO_IMAGE_FILENAME)
    with open(file_path, "wb") as f:
        f.write(png)
    return file_path
# Routes
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        app.logger.exception(f"handler error: {e}")
        abort(400)
    return "OK"
@app.route("/lotto/latest_clean.png", methods=["GET"])
def lotto_latest_clean():
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    file_path = os.path.join(STATIC_DIR, LOTTO_IMAGE_FILENAME)
    if os.path.exists(file_path):
        with open(file_path, "rb") as f:
            png = f.read()
        return Response(png, mimetype="image/png", headers=headers)
    data = fetch_latest_lotto()
    if not data:
        img = Image.new("RGB", (900, 300), (255, 220, 230))
        draw = ImageDraw.Draw(img)
        font = _load_font(36, bold=True)
        draw.text((450, 150), "ยังดึงผลหวยไม่ได้ / ยังไม่ออกผล", font=font, fill=(0, 0, 0), anchor="mm")
        buf = BytesIO()
        img.save(buf, format="PNG")
        return Response(buf.getvalue(), mimetype="image/png", headers=headers)
    png = render_lotto_image_clean(data)
    try:
        with open(file_path, "wb") as f:
            f.write(png)
    except Exception as e:
        app.logger.warning(f"save lotto image failed in endpoint: {e}")
    return Response(png, mimetype="image/png", headers=headers)
# Handlers
@handler.add(MessageEvent, message=TextMessageContent)
def on_text(event: MessageEvent):
    text = (event.message.text or "").strip()
    dc = getattr(event, "delivery_context", None)
    if dc and getattr(dc, "is_redelivery", False):
        app.logger.info("skip redelivery event")
        return
    # จำกลุ่มถ้าเปิดโหมดจำ (ให้พิมอะไรก็ได้)
    remember_target(event)
    # รับเฉพาะคำสั่งที่ขึ้นต้นด้วย / สำหรับการประมวลผลคำสั่ง
    if not text.startswith("/"):
        return
    exclude_id = current_target_id(event)
# ---------------- ลูกค้า ----------------
    if text == "/ลูกค้า":
        full_text = build_customers_text()
        # ถ้าข้อความสั้น ส่งเลย
        if len(full_text) < 4500:
            safe_send(event, [TextMessage(text=full_text)])
            return
        # ถ้าข้อยาวเกิน ให้ตัดแบ่งเป็นท่อนๆ (Chunk)
        lines = full_text.split('\n')
        chunks = []
        current_chunk = ""
        for line in lines:
            # ถ้าเอาบรรทัดใหม่ไปต่อแล้วเกิน 4000 ตัว ให้ตัดท่อนเก็บไว้ก่อน
            if len(current_chunk) + len(line) + 1 > 4000:
                chunks.append(current_chunk)
                current_chunk = line + "\n"
            else:
                current_chunk += line + "\n"
        if current_chunk:
            chunks.append(current_chunk)
        # สร้าง Message Object จากท่อนที่ตัดไว้ (ส่งได้สูงสุด 5 บับเบิ้ลต่อครั้ง)
        messages = [TextMessage(text=c.strip()) for c in chunks[:5]]
        safe_send(event, messages)
        return
    if text.startswith("/ลูกค้า"):
        parts = text.split()
        if len(parts) == 1:
            safe_send(event, [TextMessage(text=build_customers_text())])
            return
        sub = parts[1].strip()
        if sub == "เปิด":
            set_remember_enabled(True)
            safe_send(event, [TextMessage(text="✅ เปิดโหมดจำชื่อ/ID (เฉพาะกลุ่ม) แล้วครับ\n(จากนี้เมื่อมีข้อความเข้ากลุ่ม จะเริ่มบันทึกลง targets.json)")])
            return
        if sub == "ปิด":
            set_remember_enabled(False)
            safe_send(event, [TextMessage(text="⛔ ปิดโหมดจำชื่อ/ID (เฉพาะกลุ่ม) แล้วครับ\n(จะไม่บันทึกเพิ่ม แต่รายชื่อเดิมยังอยู่ใน targets.json)")])
            return

        safe_send(event, [TextMessage(text="ใช้คำสั่ง:\n- /ลูกค้า\n- /ลูกค้า เปิด\n- /ลูกค้า ปิด")])
        return
    # ---------------- ปิดรับ / แจ้งโอน ----------------
    if text == "/ปิดรับ":
        # ใส่ Link ที่ได้จากเว็บฝากรูปตรงนี้
        url = "https://i.postimg.cc/WtcRzDxG/close.jpg" 
        msg = ImageMessage(original_content_url=url, preview_image_url=url)
        safe_send(event, [msg])
        push_to_all([msg], exclude_id=exclude_id)
        return
    if text == "/แจ้งโอน":
        # ใส่ Link ที่ได้จากเว็บฝากรูปตรงนี้
        url = "https://i.postimg.cc/d1QGM41P/transferv.jpg"
        msg = ImageMessage(original_content_url=url, preview_image_url=url)
        safe_send(event, [msg])
        push_to_all([msg], exclude_id=exclude_id)
        return
    # ---------------- ส่งผลหวย (แบบระบุ URL ท้ายคำสั่ง) ----------------
    if text.startswith("/ส่งผลหวย"):
        # แยกคำสั่งกับลิงก์ออกจากกัน
        parts = text.split()
        # 1. เช็กว่าใส่ลิงก์มาหรือเปล่า? (ถ้าพิมพ์มาแค่ /ส่งผลหวย ให้เตือน)
        if len(parts) < 2:
            safe_send(event, [TextMessage(text="⚠️ กรุณาใส่ลิงก์รูปต่อท้ายคำสั่งด้วยครับ\n\nตัวอย่าง:\n/ส่งผลหวย https://i.postimg.cc/ตัวอย่าง/lotto.jpg")])
            return
        # ดึงลิงก์จากข้อความส่วนที่ 2
        url = parts[1].strip()
        # 2. ตรวจสอบว่าเป็น HTTPS และเป็นไฟล์รูปหรือไม่
        if not url.lower().startswith("https://"):
            safe_send(event, [TextMessage(text="⚠️ ลิงก์รูปต้องขึ้นต้นด้วย https:// เท่านั้นครับ")])
            return
        # (ตรวจสอบนามสกุลไฟล์เพิ่ม เพื่อความชัวร์)
        if not (url.endswith(".jpg") or url.endswith(".png") or url.endswith(".jpeg")):
             safe_send(event, [TextMessage(text="⚠️ ลิงก์ดูเหมือนไม่ใช่รูปภาพ (ต้องลงท้ายด้วย .jpg หรือ .png)\nตรวจสอบว่าเป็น 'Direct Link' หรือไม่ครับ")])
             return
        # 3. สร้างข้อความรูปภาพ
        msg = ImageMessage(original_content_url=url, preview_image_url=url)
        # 4. ส่งให้แอดมินดูตัวอย่างก่อน 1 รอบ
        safe_send(event, [msg])
        push_to_all([msg], exclude_id=exclude_id)
        safe_send(event, [TextMessage(text="✅ ระบบกำลังทยอยส่งรูปไปยังทุกกลุ่มครับ")])
        return
    # ---------------- ผลหวย ----------------
    if text == "/ผลหวย":
        # 1. เช็กว่าตั้งค่า BASE_URL หรือยัง
        if not _is_https(BASE_URL):
            safe_send(event, [TextMessage(text="⚠️ ต้องตั้งค่า BASE_URL เป็น https ก่อนครับ ถึงจะส่งรูปให้ดูได้")])
            return
        # 2. พยายามดึงข้อมูลหวย
        data = fetch_latest_lotto(force=True)
        if not data:
            safe_send(event, [TextMessage(text="ยังดึงผลหวยไม่ได้ / หรือผลอาจจะยังไม่ออกครับ")])
            return
        try:
            # 3. สร้างรูปและบันทึก
            save_lotto_image_to_static(data)
            # 4. สร้าง URL ของรูป (ต้องมี BASE_URL + path ของรูป)
            # เติม ?t=... เพื่อให้ LINE รู้ว่าเป็นรูปใหม่เสมอ (ไม่ cached รูปเก่า)
            url = f"{BASE_URL}{LOTTO_IMAGE_PATH}?t={int(time.time())}"
            # 5. เตรียมข้อความรูปภาพ
            msg = ImageMessage(original_content_url=url, preview_image_url=url)
            # 6. ส่งกลับหาคนสั่ง
            safe_send(event, [msg])
        except Exception as e:
            app.logger.exception(f"save lotto image failed: {e}")
            safe_send(event, [TextMessage(text=f"เกิดข้อผิดพลาดในการสร้างรูป: {e}")])
        return
    # help
    safe_send(event, [TextMessage(text="คำสั่งที่ใช้ได้:\n- /ลูกค้า\n- /ลูกค้า เปิด\n- /ลูกค้า ปิด\n- /ปิดรับ\n- /แจ้งโอน\n- /ผลหวย\n- /ส่งผลหวย")])
    return
if __name__ == "__main__":
    app.run(port=5000, debug=True)

