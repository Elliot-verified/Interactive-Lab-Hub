import time
import shutil
import subprocess
from datetime import datetime

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
        subprocess.Popen([TTS, "-s", "150", "-a", "400", text], 
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def spoken_time(now):
    hour = now.strftime("%-I")
    ampm = now.strftime("%p")
    if now.minute == 0:
        return f"It's {hour} o'clock {ampm}"
    if now.minute < 10:
        return f"It's {hour} oh {now.minute} {ampm}"
    return f"It's {hour} {now.minute} {ampm}"


def text_width(text, font):
    left, _, right, _ = font.getbbox(text)
    return right - left


# ---------- Main loop ----------
last_spoken_minute = None

while True:
    now = datetime.now()

    # Speak once, at the top of each minute
    if now.minute != last_spoken_minute:
        speak(spoken_time(now))
        last_spoken_minute = now.minute

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

    # Date along the bottom
    date_str = now.strftime("%A, %B %-d")
    draw.text(((width - text_width(date_str, font_date)) // 2, height - 26),
              date_str, font=font_date, fill=DIM)

    disp.image(image, rotation)
    time.sleep(0.2)