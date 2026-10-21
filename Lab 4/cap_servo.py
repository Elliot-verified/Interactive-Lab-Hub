#!/usr/bin/env python3
"""Touch a capacitive pad to point the servo at a cardinal direction.

Four contacts on the MPR121 capacitive sensor each snap the servo to one angle,
turning the Feast Automata lazy Susan toward a direction. Touch a pad and the
turntable turns there.

Hardware (both on I2C, confirmed with `i2cdetect -y 1`):
  - MPR121 capacitive touch sensor   @ 0x5A
  - SparkFun Servo pHAT (PCA9685)    @ 0x40, servo on SERVO_CH

Run it:
    python cap_servo.py

The Servo pHAT needs its own USB-C power -- don't drive the servo from the Pi.
"""

import time

import board
import busio
import adafruit_mpr121
import pi_servo_hat

SERVO_CH = 0        # Servo pHAT channel the servo is plugged into

# (capacitive pad, direction label, servo angle).
# A standard servo only spans 0-180 degrees, so the four "cardinal" positions
# are spread across that range. Change the pad numbers to match which four
# contacts you actually wired, and the angles to taste.
DIRECTIONS = [
    (0, "North", 0),
    (1, "East", 60),
    (2, "South", 120),
    (3, "West", 180),
]


def main() -> None:
    i2c = busio.I2C(board.SCL, board.SDA)
    cap = adafruit_mpr121.MPR121(i2c)

    servo = pi_servo_hat.PiServoHat()
    servo.restart()

    pads = {pad: (label, angle) for pad, label, angle in DIRECTIONS}
    was_touched = {pad: False for pad in pads}

    # Start pointed at the first direction.
    servo.move_servo_position(SERVO_CH, DIRECTIONS[0][2])
    print("Touch a pad to turn the servo. Ctrl-C to stop.")
    for pad, label, angle in DIRECTIONS:
        print(f"  pad {pad} -> {label} ({angle}°)")

    try:
        while True:
            for pad, (label, angle) in pads.items():
                touched = cap[pad].value
                # Act only on a fresh touch (rising edge), not while held.
                if touched and not was_touched[pad]:
                    servo.move_servo_position(SERVO_CH, angle)
                    print(f"{label}: {angle}°")
                was_touched[pad] = touched
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
