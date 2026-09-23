import time
import random
import shutil
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import board
import digitalio
from PIL import Image, ImageDraw, ImageFont
import adafruit_rgb_display.st7789 as st7789

# ---------- Display setup ----------
cs_pin = digitalio.DigitalInOut(board.D5)
dc_pin = digitalio.DigitalInOut(board.D25)
reset_pin = None
BAUDRATE = 64000000
spi = board.SPI()

disp = st7789.ST7789(
    spi, cs=cs_pin, dc=dc_pin, rst=reset_pin, baudrate=BAUDRATE,
    width=135, height=240, x_offset=53, y_offset=40,
)

height = disp.width   # swapped for landscape: 240 x 135
width = disp.height
rotation = 90
image = Image.new("RGB", (width, height))
draw = ImageDraw.Draw(image)

backlight = digitalio.DigitalInOut(board.D22)
backlight.switch_to_output()
backlight.value = True

# ---------- Buttons (MiniPiTFT: A = D23, B = D24, active-low) ----------
button_a = digitalio.DigitalInOut(board.D23)   # cycle time zone
button_b = digitalio.DigitalInOut(board.D24)   # bottom button: "Tatiana is mean"
for b in (button_a, button_b):
    b.switch_to_input(pull=digitalio.Pull.UP)

# ---------- Fonts ----------
FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
font_time = ImageFont.truetype(FONT_DIR + "DejaVuSans-Bold.ttf", 36)
font_small = ImageFont.truetype(FONT_DIR + "DejaVuSans.ttf", 14)
font_tiny = ImageFont.truetype(FONT_DIR + "DejaVuSans.ttf", 12)

BG = (0, 0, 0)
FG = (255, 60, 40)      # classic red-LED alarm-clock colour
DIM = (120, 120, 120)

# ---------- Layout: face on the left, clock on the right ----------
FACE_SIZE = 115                       # square, nearly full height
FACE_X, FACE_Y = 6, (height - FACE_SIZE) // 2
CLOCK_X0 = FACE_X + FACE_SIZE + 6     # left edge of the clock column
CLOCK_W = width - CLOCK_X0 - 4

# ---------- Ageing face pictures (change every 10 s) ----------
AGES = [1, 10, 16, 25, 40, 70]
FACE_FILES = [f"elliot_age_{a:02d}.png" for a in AGES]   # same dir as script
FACE_INTERVAL = 10                    # seconds per age

faces = []
for f in FACE_FILES:
    im = Image.open(f).convert("RGB")
    im.thumbnail((FACE_SIZE, FACE_SIZE), Image.BICUBIC)
    faces.append(im)

start_time = time.monotonic()

# ---------- Time zones ----------
ZONES = [
    ("New York",  "America/New_York"),
    ("Chicago",   "America/Chicago"),
    ("Denver",    "America/Denver"),
    ("Los Angeles", "America/Los_Angeles"),
    ("London",    "Europe/London"),
    ("Paris",     "Europe/Paris"),
    ("Dubai",     "Asia/Dubai"),
    ("Mumbai",    "Asia/Kolkata"),
    ("Singapore", "Asia/Singapore"),
    ("Tokyo",     "Asia/Tokyo"),
    ("Sydney",    "Australia/Sydney"),
]
zone_index = 0

# ---------- Quotes on the passage of time (button B) ----------
QUOTES = [
    "Time is the wisest counselor of all. Pericles",
    "Lost time is never found again. Benjamin Franklin",
    "It is not that we have a short time to live, but that we waste a lot of it. Seneca",
    "Time is the most valuable thing a man can spend. Theophrastus",
    "Time and tide wait for no man. English proverb",
    "The best time to plant a tree was twenty years ago. The second best time is now. Chinese proverb",
    "Time flies over us, but leaves its shadow behind. Nathaniel Hawthorne",
    "Do not squander time, for that is the stuff life is made of. Benjamin Franklin",
    "Time discovers truth. Seneca",
    "Time is a river of passing events, and strong is its current. Marcus Aurelius",
    "Seize the day, trusting as little as possible in tomorrow. Horace",
    "Time brings all things to pass. Aeschylus",
    "The two most powerful warriors are patience and time. Leo Tolstoy",
    "You may delay, but time will not. Benjamin Franklin",
    "Nothing endures but change. Heraclitus",
    "Time heals what reason cannot. Seneca",
    "All that we are is the result of what we have thought. Buddha",
    "To everything there is a season, and a time to every purpose under heaven. Ecclesiastes",
    "Better three hours too soon than a minute too late. Shakespeare",
    "Time is but the stream I go a-fishing in. Henry David Thoreau",
]

# ---------- Speech ----------
# Uses espeak-ng if installed, otherwise espeak:  sudo apt install espeak-ng
TTS = shutil.which("espeak-ng") or shutil.which("espeak")


def speak(text):
    """Speak without blocking the display loop."""
    if TTS:
        subprocess.Popen([TTS, "-s", "150", "-a", "200", text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def spoken_time(now, city):
    hour = now.strftime("%-I")
    ampm = now.strftime("%p")
    if now.minute == 0:
        t = f"{hour} o'clock {ampm}"
    elif now.minute < 10:
        t = f"{hour} oh {now.minute} {ampm}"
    else:
        t = f"{hour} {now.minute} {ampm}"
    return f"It's {t} in {city}"


def text_width(text, font):
    left, _, right, _ = font.getbbox(text)
    return right - left


def centered_x(text, font):
    return CLOCK_X0 + (CLOCK_W - text_width(text, font)) // 2


# ---------- Main loop ----------
last_spoken_minute = None
prev_a = prev_b = True

while True:
    city, tz_name = ZONES[zone_index]
    now = datetime.now(ZoneInfo(tz_name))

    # Buttons: fire once per press (high -> low edge)
    a, b = button_a.value, button_b.value
    a_pressed = prev_a and not a
    b_pressed = prev_b and not b
    prev_a, prev_b = a, b

    if a_pressed:
        zone_index = (zone_index + 1) % len(ZONES)
        city, tz_name = ZONES[zone_index]
        now = datetime.now(ZoneInfo(tz_name))
        speak(city)
        time.sleep(0.15)

    if b_pressed:                       # bottom button
        speak("Tatiana is mean")
        time.sleep(0.15)

    # Speak the time at the top of each minute
    if now.minute != last_spoken_minute:
        last_spoken_minute = now.minute
        speak(spoken_time(now, city))

    # Which age to show right now
    elapsed = time.monotonic() - start_time
    face_index = int(elapsed // FACE_INTERVAL) % len(faces)
    face = faces[face_index]

    # ----- Draw -----
    draw.rectangle((0, 0, width, height), fill=BG)

    # Face on the left, centred in its square
    image.paste(face, (FACE_X + (FACE_SIZE - face.width) // 2,
                       FACE_Y + (FACE_SIZE - face.height) // 2))
    draw.text((FACE_X + 4, FACE_Y + FACE_SIZE - 16), f"age {AGES[face_index]}",
              font=font_tiny, fill=(255, 255, 255))

    # Clock on the right: blinking colon, AM/PM, seconds, city, date
    colon = ":" if now.second % 2 == 0 else " "
    big = now.strftime(f"%-I{colon}%M")
    draw.text((centered_x(big, font_time), 22), big, font=font_time, fill=FG)

    sub = now.strftime("%p") + "  " + now.strftime("%S")
    draw.text((centered_x(sub, font_small), 66), sub, font=font_small, fill=DIM)

    draw.text((centered_x(city, font_small), 92), city, font=font_small, fill=DIM)
    date_str = now.strftime("%a %b %-d")
    draw.text((centered_x(date_str, font_tiny), 112), date_str, font=font_tiny, fill=DIM)

    disp.image(image, rotation)
    time.sleep(0.05)