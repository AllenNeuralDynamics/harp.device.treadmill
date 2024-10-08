#!/usr/bin/env python3
"""Calibration script for extacting raw data to linearize magnetic brake."""
# Uses: Jrk G2 18v27 Pololu motor controller to apply torque to the brake.
# https://www.pololu.com/docs/0J73

from datetime import datetime
from pyharp.device import Device
from pyharp.messages import HarpMessage
from pathlib import Path
from time import sleep, perf_counter
from ruamel.yaml import YAML
from serial import SerialException
from struct import unpack
from tqdm import tqdm
import matplotlib.pyplot as plt
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
import subprocess
import os
import sys
import numpy as np

ALL_SENSORS_REG = 35
BRAKE_CURRENT_SETPOINT_REG = 37
TORQUE_LIMITING_TRIGGERED_REG = 41

device = None

def jrk2cmd(*args):
    reply = subprocess.check_output(['jrk2cmd'] + list(args))
    yaml = YAML(typ="safe", pure=True)
    return yaml.load(reply)

def get_motor_controller_settings() -> dict:
    """load motor settings to dict."""
    # Skip writing to file by writing to stdout and collecting the output.
    yaml = YAML(typ="safe", pure=True)
    settings = None
    if os.name == 'nt': # Windows requires that we write the result to a file.
        settings_file = Path("./tmp.txt")
        subprocess.check_output(["jrk2cmd", "--get-settings",
                                 settings_file.resolve()])
        settings = yaml.load(settings_file)
        settings_file.unlink()
    else:
        reply_str = subprocess.check_output(["jrk2cmd", "--get-settings",
                                            "/dev/stdout"])
        settings = yaml.load(reply_str)
    return settings

def apply_motor_controller_settings(settings: dict, device_num: int = 0):
    """Write settings to the device and reinitialize it with those settings."""
    # Skip writing to file by reading data from stdin.
    yaml = YAML(typ=['rt', 'string'])
    settings_str = yaml.dump_to_string(settings)
    # FIXME: should be a way to do this without writing/deleting a tmp file.
    filename = ".settings.yml"
    with open(filename, "w") as f:
        f.write(yaml.dump_to_string(settings))
    reply_str = subprocess.check_output(["jrk2cmd", "--settings", f"{filename}"])
    os.remove(filename) # remove temporary file.
    # Re-initialize to apply the settings on the device.
    jrk2cmd("--reinitialize")

def set_motor_to_open_loop_mode(settings: dict):
    """Modify settings to put the motor controller in open loop mode."""
    # Check for any intermittent settings files.
    settings["feedback_mode"] = "none"  # This field explicitly wants a string.

def set_motor_current_limit(settings: dict, current_limit_percent: float):
    """Set motor current limit in percent. 0 = no limit."""
    max_uint16 = np.iinfo(np.uint16).max
    current_limit = max_uint16 * current_limit_percent / 100.0
    settings["soft_current_limit_forward"] = round(current_limit)
    settings["soft_current_limit_reverse"] = round(current_limit)

def set_signed_motor_speed(percent: float):
    """Set the motor speed in the percent range [-100: 100]."""
    # Map values to signed range 0 - 4095 where 2048 is midscale.
    # Full Scale Range for input speed is [-600:600].
    speed = round((600/100.0)*percent)
    jrk2cmd("--speed", f"{speed}")

def set_brake_current_raw(raw_current):
    """Set the brake current in raw uint16 format."""
    device.send(HarpMessage.WriteU16(BRAKE_CURRENT_SETPOINT_REG,
                                     raw_current).frame)

def clear_treadmill_overtorque_errors():
    device.send(HarpMessage.WriteU8(TORQUE_LIMITING_TRIGGERED_REG, 0).frame)

def get_measurements() -> list:
    """Read the [position, torque, current] measurements from the board."""
    bulk_meas = device.send(HarpMessage.ReadS32(ALL_SENSORS_REG).frame)
    vals = unpack("<lll", bulk_meas._raw_payload)
    return vals
    #return [0, int(perf_counter()/1000), 0]


if __name__ == "__main__":
    # Collect input params.
    # Show defaults.
    parser = ArgumentParser(formatter_class=ArgumentDefaultsHelpFormatter)
    parser.add_argument("--port", type=str, default="/dev/ttyACM0",
                        help="Harp Treadmill Driver com port")
    parser.add_argument("--min_current", type=float, default=0,
                        help="minimum current value in percent of full scale "
                             "range.")
    parser.add_argument("--max_current", type=float, default=40,
                        help="maximum current value in percent of full scale "
                             "range.")
    parser.add_argument("--sample_average_count", type=int, default=30,
                        help="number of samples to average together "
                             "*per* data point.")
    parser.add_argument("--time_delta", type=float, default=0.01,
                        help="time (in seconds) between collecting samples to "
                             "average.")
    parser.add_argument("--reverse", type=bool, default=False)
    parser.add_argument("--output_file", type=str, default=None)
    args = parser.parse_args()

    # Connect to Harp Treadmill Device.
    try:
        null_file = "/dev/null"
        if os.name == 'nt':
            null_file = "NUL"
        device = Device(args.port, null_file)
    except SerialException as e:
        print("Cannot connect to Harp Treadmill device! Is it plugged in and "
              "powered on? Is the com port correct?")
        sys.exit(1) # Return with error.

    # Configure motor controller
    print("Reading motor settings.")
    settings = get_motor_controller_settings()
    print("Adjusting settings.")
    set_motor_to_open_loop_mode(settings)
    set_motor_current_limit(settings, 0)
    print("Writing back new settings.")
    apply_motor_controller_settings(settings)
    sleep(0.1)
    clear_treadmill_overtorque_errors()
    print("Setting testing motor to max open loop speed.")
    reverse = -1.0 if args.reverse else 1.0
    set_signed_motor_speed(100. * reverse)
    sleep(1.0)

    # Make X/Y ranges.
    max_uint16 = np.iinfo(np.uint16).max
    # Range should be in steps of 4 since the value is 12-bit upscaled to 16-bit
    input_current = np.arange(0, round(max_uint16*args.max_current/100.), 4)
    output_torque = np.zeros(len(input_current))

    print("Starting calibration at "
          f"{datetime.now().strftime('%Y-%m-%d, %I:%M %p')}")
    eta_mins = round(args.sample_average_count * args.time_delta
                     * len(input_current) / 60)
    print(f"Calibration will take at least {eta_mins} minutes.")

    # Collect data.
    try:
        for i, current in enumerate(tqdm(input_current)):
            set_brake_current_raw(int(current))
            raw_pts = []
            for _ in range(args.sample_average_count):
                sleep(args.time_delta)
                raw_pts.append(get_measurements()[1])
            output_torque[i] = float(sum(raw_pts))/args.sample_average_count
        print()
    except KeyboardInterrupt:
        pass # abort early and print what we have.
    finally:
        print("Zeroing motor speed.")
        set_signed_motor_speed(0) # %
        set_brake_current_raw(0)

    # Optional: save the data.
    if args.output_file is not None:
        np.savetxt(args.output_file, np.array((input_current, output_torque)).T,
        delimiter=", ", fmt="%1.6f")

    # Plot Output current vs input current!
    plt.title("Brake Torque vs Input Current")
    plt.xlabel(f"input torque [0:{args.max_current}%] Full-Scale Range")
    plt.ylabel("brake torque [0:4095]\r\nFull-Scale Range w/2048 midscale")
    plt.plot(input_current, output_torque)
    plt.show()

