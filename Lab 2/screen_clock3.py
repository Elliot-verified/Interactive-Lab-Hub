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

# ---------- Display setup (unchanged from the original) ----------
cs_pin = digitalio.DigitalInOut(board.D5)
dc_pin = digitalio.DigitalInOut(board.D25)
reset_pin = None
BAUDRATE = 64000000
spi = board.SPI()

disp = st7789.ST7789(
    spi, cs=cs_pin, dc=dc_pin, rst=reset_pin, baudrate=BAUDRATE,
    width=135, height=240, x_offset=53, y_offset=40,
)

height = disp.width   # swapped for landscape
width = disp.height
rotation = 90
image = Image.new("RGB", (width, height))
draw = ImageDraw.Draw(image)

backlight = digitalio.DigitalInOut(board.D22)
backlight.switch_to_output()
backlight.value = True

# ---------- Buttons (MiniPiTFT: A = D23, B = D24, active-low) ----------
button_a = digitalio.DigitalInOut(board.D23)   # cycle time zone
button_b = digitalio.DigitalInOut(board.D24)   # speak the time
for b in (button_a, button_b):
    b.switch_to_input(pull=digitalio.Pull.UP)

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

# ---------- Fonts ----------
FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
font_time = ImageFont.truetype(FONT_DIR + "DejaVuSans-Bold.ttf", 64)
font_small = ImageFont.truetype(FONT_DIR + "DejaVuSans.ttf", 18)
font_date = ImageFont.truetype(FONT_DIR + "DejaVuSans.ttf", 16)

BG = (0, 0, 0)
FG = (255, 60, 40)      # classic red-LED alarm-clock colour
DIM = (120, 120, 120)

# ---------- Speech ----------
# Uses espeak-ng if installed, otherwise espeak.  Install with:
#   sudo apt install espeak-ng
TTS = shutil.which("espeak-ng") or shutil.which("espeak")


def speak(text):
    """Speak without blocking the display loop."""
    if TTS:
        subprocess.Popen([TTS, "-s", "150", "-a", "200", text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------- Face picture (shown for a few seconds each minute) ----------
FACE_PATH = "elliot.png"      # same directory as this script
FACE_SECONDS = 4              # how long the face stays up

face = Image.open(FACE_PATH).convert("RGB")
face.thumbnail((width, height), Image.BICUBIC)   # scale to fit, keep proportions
face_canvas = Image.new("RGB", (width, height), BG)
face_canvas.paste(face, ((width - face.width) // 2, (height - face.height) // 2))


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


# ---------- Main loop ----------
last_spoken_minute = None
prev_a = prev_b = True          # buttons idle high (not pressed)

while True:
    city, tz_name = ZONES[zone_index]
    now = datetime.now(ZoneInfo(tz_name))

    # Read buttons and detect the press edge (high -> low)
    a, b = button_a.value, button_b.value
    a_pressed = prev_a and not a
    b_pressed = prev_b and not b
    prev_a, prev_b = a, b

    if a_pressed:                       # button A: next time zone
        zone_index = (zone_index + 1) % len(ZONES)
        city, tz_name = ZONES[zone_index]
        now = datetime.now(ZoneInfo(tz_name))
        speak(city)
        time.sleep(0.15)                # debounce

    if b_pressed:                       # button B: a random quote about time
        speak(random.choice(QUOTES))
        time.sleep(0.15)

    # At the top of each minute: speak the time and flash the face
    if now.minute != last_spoken_minute:
        last_spoken_minute = now.minute
        speak(spoken_time(now, city))
        disp.image(face_canvas, rotation)
        time.sleep(FACE_SECONDS)
        continue   # go straight back to drawing the clock

    draw.rectangle((0, 0, width, height), fill=BG)

    # Big HH:MM with a colon that blinks every second
    colon = ":" if now.second % 2 == 0 else " "
    big = now.strftime(f"%-I{colon}%M")
    ampm = now.strftime("%p")

    big_w = text_width(big, font_time)
    ampm_w = text_width(ampm, font_small)
    total_w = big_w + 8 + ampm_w
    x0 = (width - total_w) // 2
    y0 = 18

    draw.text((x0, y0), big, font=font_time, fill=FG)
    draw.text((x0 + big_w + 8, y0 + 12), ampm, font=font_small, fill=FG)

    # Seconds, small, under the AM/PM
    secs = now.strftime("%S")
    draw.text((x0 + big_w + 8, y0 + 36), secs, font=font_small, fill=DIM)

    # City and date along the bottom
    date_str = f"{city}  ·  " + now.strftime("%a, %b %-d")
    draw.text(((width - text_width(date_str, font_date)) // 2, height - 26),
              date_str, font=font_date, fill=DIM)

    disp.image(image, rotation)
    time.sleep(0.05)   # fast poll so button presses register