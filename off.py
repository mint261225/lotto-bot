# -*- coding: utf-8 -*-
import os
import re
import time
from io import BytesIO
from typing import Any, Dict, List, Optional
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
    BroadcastRequest,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    ImageMessage,
)
from linebot.v3.messaging.exceptions import ApiException

load_dotenv()

# รับค่าจาก Environment Variables (บน Render)
CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN", "").strip()
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip()

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)
LOTTO_IMAGE_FILENAME = "lotto_latest.png"
LOTTO_IMAGE_PATH = f"/static/{LOTTO_IMAGE_FILENAME}"

app = Flask(__name__)

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    print("⚠️ Warning: Token/Secret not found. Make sure to set Env Vars on Render.")

handler = WebhookHandler(CHANNEL_SECRET)
config = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
ZERO_REPLY_TOKEN = "00000000000000000000000000000000"

# ------------------------
# Utilities
# ------------------------
def _is_https(url: str) -> bool:
    return isinstance(url, str) and url.lower().startswith("https://")

def current_target_id(event: MessageEvent) -> Optional[str]:
    """หา ID ผู้รับ (Group/Room/User) สำหรับ fallback กรณี Reply ไม่ได้"""
    src = event.source
    return getattr(src, "group_id", None) or getattr(src, "room_id", None) or getattr(src, "user_id", None)

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
    to_id = current_target_id(event)
    reply_token = getattr(event, "reply_token", None)
    
    if (not reply_token) or (reply_token == ZERO_REPLY_TOKEN):
        if to_id:
            try:
                push_messages(to_id, messages)
            except Exception as e:
                print(f"Push failed: {e}")
        return
    try:
        reply_messages(reply_token, messages)
    except ApiException:
        # ถ้า Reply fail ให้ลอง Push แทน
        if to_id:
            try:
                push_messages(to_id, messages)
            except Exception:
                pass
    except Exception as e:
        print(f"Reply failed: {e}")

def broadcast_to_all(messages: List[Any]) -> None:
    """ใช้ Broadcast API ส่งหาทุกคน/ทุกกลุ่ม ที่เป็นเพื่อนกับบอท"""
    try:
        with ApiClient(config) as api_client:
            api = MessagingApi(api_client)
            req = BroadcastRequest(messages=messages)
            api.broadcast(req)
            print("✅ Broadcast sent successfully.")
    except Exception as e:
        print(f"❌ Broadcast failed: {e}")

# ------------------------
# Fonts (Cloud Optimized)
# ------------------------
# ใช้เฉพาะฟอนต์ในโฟลเดอร์ fonts/ เท่านั้น (ตัด Windows Path ออกเพื่อกัน Error บน Linux)
FONT_REGULAR_PATH = os.path.join(BASE_DIR, "fonts", "Sarabun-Regular.ttf")
FONT_BOLD_PATH = os.path.join(BASE_DIR, "fonts", "Sarabun-Bold.ttf")

def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD_PATH if bold else FONT_REGULAR_PATH
    try:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    except Exception:
        pass
    return ImageFont.load_default()

# ------------------------
# Lotto Fetching
# ------------------------
LOTTERY_CO_TH_URL = "https://www.lottery.co.th/"
SANOOK_ICHECK_URL = "https://news.sanook.com/lotto/icheck/"
THAI_MONTHS_ABBR = {
    "ม.ค.": "มกราคม", "ก.พ.": "กุมภาพันธ์", "มี.ค.": "มีนาคม", "เม.ย.": "เมษายน",
    "พ.ค.": "พฤษภาคม", "มิ.ย.": "มิถุนายน", "ก.ค.": "กรกฎาคม", "ส.ค.": "สิงหาคม",
    "ก.ย.": "กันยายน", "ต.ค.": "ตุลาคม", "พ.ย.": "พฤศจิกายน", "ธ.ค.": "ธันวาคม",
}

def _normalize_date_th_from_short(short_date: str) -> str:
    s = (short_date or "").strip()
    m = re.search(r"(\d{1,2})\s+([ก-๙]{1,4}\.)\s+(\d{2})", s)
    if not m: return s
    day = int(m.group(1))
    mon_abbr = m.group(2)
    yy = int(m.group(3))
    mon_full = THAI_MONTHS_ABBR.get(mon_abbr, mon_abbr)
    be_year = 2500 + yy
    return f"{day} {mon_full} {be_year}"

def fetch_lotto_from_lottery_co_th() -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(LOTTERY_CO_TH_URL, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        txt = soup.get_text("\n", strip=True)
        pat = re.compile(r"(\d{1,2}\s+[ก-๙]{1,4}\.\s+\d{2}).{0,120}?(\d{6})\s+(\d{2})\s+(\d{3})\s+(\d{3})\s+(\d{3})\s+(\d{3})")
        m = pat.search(txt)
        if not m: return None
        return {
            "date_th": _normalize_date_th_from_short(m.group(1)),
            "first": m.group(2),
            "last2": m.group(3),
            "last3": [m.group(4), m.group(5)],
            "front3": [m.group(6), m.group(7)],
        }
    except Exception:
        return None

def fetch_lotto_from_sanook_icheck() -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(SANOOK_ICHECK_URL, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        txt = BeautifulSoup(r.text, "html.parser").get_text("\n", strip=True)
        pattern = re.compile(r"(\d{1,2}\s+\S+\s+\d{4}).{0,1200}?รางวัลที่ 1\s+(\d{6}).{0,800}?เลขหน้า 3 ตัว\s+(\d{3})\s+(\d{3}).{0,800}?เลขท้าย 3 ตัว\s+(\d{3})\s+(\d{3}).{0,800}?เลขท้าย 2 ตัว\s+(\d{2})", re.S)
        m = pattern.search(txt)
        if not m: return None
        return {
            "date_th": m.group(1),
            "first": m.group(2),
            "front3": [m.group(3), m.group(4)],
            "last3": [m.group(5), m.group(6)],
            "last2": m.group(7),
        }
    except Exception:
        return None

def fetch_latest_lotto() -> Optional[Dict[str, Any]]:
    # บน Cloud Server ไม่ต้อง Cache นาน เพราะ Server อาจ restart บ่อย
    # แต่ Cache ไว้ 60 วินาทีเพื่อกันยิงซ้ำๆ
    for fn in [fetch_lotto_from_lottery_co_th, fetch_lotto_from_sanook_icheck]:
        data = fn()
        if data: return data
    return None

# ------------------------
# Image Rendering
# ------------------------
def render_lotto_image_clean(data: Dict[str, Any]) -> bytes:
    W, H = 1200, 720
    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    
    # Gradient BG
    for y in range(H):
        t = y / max(H - 1, 1)
        r = int(245 * (1 - t) + 255 * t)
        g = int(247 * (1 - t) + 210 * t)
        b = int(250 * (1 - t) + 230 * t)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    COLOR_PINK, COLOR_TEXT, COLOR_CARD = (233, 30, 99), (30, 30, 30), (255, 255, 255)
    font_title = _load_font(52, bold=True)
    font_date = _load_font(30, bold=True)
    font_big = _load_font(120, bold=True)
    font_med = _load_font(56, bold=True)
    font_lbl = _load_font(28, bold=True)

    draw.text((W/2, 80), "ผลสลากกินแบ่งรัฐบาล", font=font_title, fill=COLOR_PINK, anchor="mm")
    
    date_th = str(data.get("date_th", "-"))
    draw.rounded_rectangle((W/2 - 180, 130, W/2 + 180, 180), radius=25, fill=COLOR_PINK)
    draw.text((W/2, 155), f"งวดประจำวันที่ {date_th}", font=font_date, fill="white", anchor="mm")

    # Layout Boxes
    boxes = [
        (70, 210, W*0.68, 520), (W*0.72, 210, W-70, 520),
        (70, 540, W*0.5-10, 690), (W*0.5+10, 540, W-70, 690)
    ]
    for b in boxes: draw.rounded_rectangle(b, radius=35, fill=COLOR_CARD, outline=(240, 180, 210), width=2)

    # Values
    def draw_section(box, title, value, font_val):
        cx = (box[0] + box[2]) / 2
        draw.text((cx, box[1] + 40), title, font=font_lbl, fill=COLOR_PINK, anchor="mm")
        draw.text((cx, box[3] - 100), value, font=font_val, fill=COLOR_TEXT, anchor="mm")

    draw_section(boxes[0], "รางวัลที่ 1", str(data.get("first", "-")), font_big)
    draw_section(boxes[1], "เลขท้าย 2 ตัว", str(data.get("last2", "-")), font_big)
    
    f3 = data.get("front3", ["-", "-"])
    draw_section(boxes[2], "เลขหน้า 3 ตัว", f"{f3[0]}   {f3[1]}", font_med)
    
    l3 = data.get("last3", ["-", "-"])
    draw_section(boxes[3], "เลขท้าย 3 ตัว", f"{l3[0]}   {l3[1]}", font_med)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def save_lotto_image_to_static(data: Dict[str, Any]) -> str:
    png = render_lotto_image_clean(data)
    with open(os.path.join(STATIC_DIR, LOTTO_IMAGE_FILENAME), "wb") as f:
        f.write(png)
    return LOTTO_IMAGE_PATH

# ------------------------
# Routes & Handlers
# ------------------------
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        print(f"Error: {e}")
        abort(400)
    return "OK"

@app.route("/lotto/latest_clean.png", methods=["GET"])
def lotto_image_endpoint():
    # Endpoint นี้จะถูกเรียกโดย LINE เพื่อแสดง Preview รูป
    # บน Render ไฟล์ใน static จะอยู่ได้ชั่วคราว (Ephemeral) ซึ่งเพียงพอสำหรับการ preview ณ ตอนนั้น
    fpath = os.path.join(STATIC_DIR, LOTTO_IMAGE_FILENAME)
    if os.path.exists(fpath):
        with open(fpath, "rb") as f:
            return Response(f.read(), mimetype="image/png")
    return "Not found", 404

@handler.add(MessageEvent, message=TextMessageContent)
def on_text(event: MessageEvent):
    text = (event.message.text or "").strip()
    
    if text == "/ปิดรับ":
        # ใส่ Link รูปปิดรับ (ถาวร)
        url = "https://i.postimg.cc/WtcRzDxG/close.jpg"
        msg = ImageMessage(original_content_url=url, preview_image_url=url)
        safe_send(event, [msg])
        broadcast_to_all([msg])
        return

    if text == "/แจ้งโอน":
        # ใส่ Link รูปแจ้งโอน (ถาวร)
        url = "https://i.postimg.cc/d1QGM41P/transferv.jpg"
        msg = ImageMessage(original_content_url=url, preview_image_url=url)
        safe_send(event, [msg])
        broadcast_to_all([msg])
        return

    if text.startswith("/ส่งผลหวย"):
        parts = text.split()
        if len(parts) < 2:
            safe_send(event, [TextMessage(text="⚠️ วิธีใช้: /ส่งผลหวย https://ลิ้งก์รูป.jpg")])
            return
        url = parts[1].strip()
        if not url.startswith("https"):
            safe_send(event, [TextMessage(text="⚠️ ลิงก์ต้องเป็น https เท่านั้น")])
            return
        
        msg = ImageMessage(original_content_url=url, preview_image_url=url)
        safe_send(event, [msg]) # ให้แอดมินดูก่อน
        broadcast_to_all([msg]) # ส่งให้ทุกคน
        safe_send(event, [TextMessage(text="✅ กำลัง Broadcast รูปไปยังทุกกลุ่ม...")])
        return

    if text == "/ผลหวย":
        if not _is_https(BASE_URL):
            safe_send(event, [TextMessage(text="⚠️ Server Config Error: BASE_URL is missing or not HTTPS.")])
            return
        
        data = fetch_latest_lotto()
        if not data:
            safe_send(event, [TextMessage(text="⏳ ยังดึงผลหวยไม่ได้ครับ")])
            return
        
        try:
            # สร้างรูปและเซฟลง Server ชั่วคราว
            save_lotto_image_to_static(data)
            
            # สร้าง URL เพื่อให้ LINE ดึงรูปไปโชว์
            # ใส่ timestamp ?t= เพื่อกัน LINE จำรูปเก่า
            image_url = f"{BASE_URL}{LOTTO_IMAGE_PATH}?t={int(time.time())}"
            
            msg = ImageMessage(original_content_url=image_url, preview_image_url=image_url)
            safe_send(event, [msg]) # ส่งกลับให้คนสั่งดูคนเดียว
        except Exception as e:
            safe_send(event, [TextMessage(text=f"Error creating image: {e}")])
        return
        
    # Help Menu
    if text == "/เมนู" or text == "/คำสั่ง":
        menu = "รายการคำสั่ง:\n/ปิดรับ : ส่งรูปปิดรับหาทุกคน\n/แจ้งโอน : ส่งรูปแจ้งโอนหาทุกคน\n/ผลหวย : ดูรูปผลหวย (แอดมินดูคนเดียว)\n/ส่งผลหวย [ลิ้งก์] : ส่งรูปลิ้งก์นั้นหาทุกคน"
        safe_send(event, [TextMessage(text=menu)])

if __name__ == "__main__":
    # ใช้สำหรับรันในเครื่องตัวเองเท่านั้น (บน Render จะใช้ Gunicorn)
    app.run(port=5000, debug=True)