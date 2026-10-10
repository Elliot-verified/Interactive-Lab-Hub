#!/usr/bin/env python3
"""Drive a servo with the rotary encoder.

Turn the knob and the servo follows, 1:1 with the physical rotation: a 180°
turn of the knob produces a 180° sweep of the servo. Clockwise raises the angle,
counter-clockwise lowers it. Press the encoder's button to snap the servo back
to center.

The 1:1 mapping assumes the encoder has 24 detents per full turn (the Adafruit
4991 does). A 180° turn is then 12 detents, so each detent is 360/24 = 15° of
servo. If your encoder has a different detent count, change DETENTS_PER_REV.

Hardware (both on I2C, confirmed with `i2cdetect -y 1`):
  - Adafruit seesaw rotary encoder  @ 0x36
  - SparkFun Servo pHAT (PCA9685)   @ 0x40, servo on channel SERVO_CH

Run it:
    python encoder_servo.py

Make sure the Servo pHAT has its own USB-C power — don't drive the servo from
the Pi's 5V rail.
"""

import time

import board
from adafruit_seesaw import seesaw, rotaryio, digitalio
import pi_servo_hat

# --- Servo range: full 0-180 so a half turn of the knob maps to a half sweep ---
SERVO_MIN = 0
SERVO_MAX = 180
SERVO_CENTER = 90
SERVO_CH = 0                 # Servo pHAT channel the servo is plugged into

# 1:1 physical mapping: servo degrees per detent = 360 / detents-per-turn.
DETENTS_PER_REV = 24         # Adafruit 4991 encoder has 24 detents per full turn
DEGREES_PER_STEP = 360 / DETENTS_PER_REV   # = 15° of servo per knob click


def clamp(value, low, high):
    return max(low, min(high, value))


def main() -> None:
    # Encoder on the seesaw board at 0x36.
    ss = seesaw.Seesaw(board.I2C(), addr=0x36)
    product = (ss.get_version() >> 16) & 0xFFFF
    if product != 4991:
        print(f"Warning: expected seesaw product 4991, found {product}. "
              "Is this the rotary encoder?")
    ss.pin_mode(24, ss.INPUT_PULLUP)      # the encoder's push button
    button = digitalio.DigitalIO(ss, 24)
    encoder = rotaryio.IncrementalEncoder(ss)

    # Servo pHAT at 0x40.
    servo = pi_servo_hat.PiServoHat()
    servo.restart()

    angle = SERVO_CENTER
    servo.move_servo_position(SERVO_CH, angle)

    # Negate so clockwise counts up, and remember where we started so the knob
    # is measured relative to this moment rather than its absolute boot count.
    last_position = -encoder.position
    button_held = False

    print(f"Ready. Turn the knob to move the servo ({SERVO_MIN}-{SERVO_MAX}°), "
          f"press to center. Ctrl-C to stop.\nServo at {angle:.0f}°")

    try:
        while True:
            position = -encoder.position
            if position != last_position:
                angle = clamp(angle + (position - last_position) * DEGREES_PER_STEP,
                              SERVO_MIN, SERVO_MAX)
                last_position = position
                servo.move_servo_position(SERVO_CH, angle)
                print(f"Servo: {angle:.0f}°")

            # Button press (active low) recenters the servo.
            if not button.value and not button_held:
                button_held = True
                angle = SERVO_CENTER
                servo.move_servo_position(SERVO_CH, angle)
                print(f"Recentered to {angle:.0f}°")
            if button.value and button_held:
                button_held = False

            time.sleep(0.01)
    except KeyboardInterrupt:
        servo.move_servo_position(SERVO_CH, SERVO_CENTER)
        print("\nStopped. Servo centered.")


if __name__ == "__main__":
    main()
