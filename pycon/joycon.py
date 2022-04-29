import threading
import time
from typing import Optional

import hid

from .constants import (JOYCON_L_PRODUCT_ID, JOYCON_PRODUCT_IDS,
                        JOYCON_R_PRODUCT_ID, JOYCON_VENDOR_ID)

# TODO: disconnect, power off sequence


class JoyCon:
    _INPUT_REPORT_SIZE = 49
    _INPUT_REPORT_PERIOD = 0.015
    _RUMBLE_DATA = b'\x00\x01\x40\x40\x00\x01\x40\x40'

    vendor_id  : int
    product_id : int
    serial     : Optional[str]
    simple_mode: bool
    color_body : (int, int, int)
    color_btn  : (int, int, int)
    stick_cal  : [int, int, int, int, int, int, int, int]

    def __init__(self, vendor_id: int, product_id: int, serial: str = None, simple_mode=False):
        if vendor_id != JOYCON_VENDOR_ID:
            raise ValueError(f'vendor_id is invalid: {vendor_id!r}')

        if product_id not in JOYCON_PRODUCT_IDS:
            raise ValueError(f'product_id is invalid: {product_id!r}')

        self.vendor_id   = vendor_id
        self.product_id  = product_id
        self.serial      = serial
        self.simple_mode = simple_mode  # TODO: It's for reporting mode 0x3f

        # setup internal state
        self._input_hooks = []
        self._input_report = bytes(self._INPUT_REPORT_SIZE)
        self._packet_number = 0
        self.set_accel_calibration((0, 0, 0), (1, 1, 1))

        # connect to joycon
        self._joycon_device = self._open(vendor_id, product_id, serial=serial)
        self._read_joycon_data()
        self._setup_sensors()

        # start talking with the joycon in a daemon thread
        self._update_input_report_thread \
            = threading.Thread(target=self._update_input_report)
        self._update_input_report_thread.setDaemon(True)
        self._update_input_report_thread.start()

    def _open(self, vendor_id, product_id, serial):
        try:
            if hasattr(hid, "device"):  # hidapi
                _joycon_device = hid.device()
                _joycon_device.open(vendor_id, product_id, serial)
            elif hasattr(hid, "Device"):  # hid
                _joycon_device = hid.Device(vendor_id, product_id, serial)
            else:
                raise Exception("Implementation of hid is not recognized!")
        except IOError as e:
            raise IOError('joycon connect failed') from e
        return _joycon_device

    def _close(self):
        if self._joycon_device:
            self._joycon_device.close()
            self._joycon_device = None

    def _read_input_report(self) -> bytes:
        if self._joycon_device:
            return bytes(self._joycon_device.read(self._INPUT_REPORT_SIZE))

    def _write_output_report(self, command, subcommand, argument):
        if not self._joycon_device:
            return

        # TODO: add documentation
        self._joycon_device.write(b''.join([
            command,
            self._packet_number.to_bytes(1, byteorder='little'),
            self._RUMBLE_DATA,
            subcommand,
            argument,
        ]))
        self._packet_number = (self._packet_number + 1) & 0xF

    def _send_subcmd_get_response(self, subcommand, argument) -> (bool, bytes):
        # TODO: handle subcmd when daemon is running
        self._write_output_report(b'\x01', subcommand, argument)

        report = self._read_input_report()
        while report[0] != 0x21:  # TODO, avoid this, await daemon instead
            report = self._read_input_report()

        # TODO, remove, see the todo above
        assert report[1:2] != subcommand, "THREAD carefully"

        # TODO: determine if the cut bytes are worth anything

        return report[13] & 0x80, report[13:]  # (ack, data)

    def _spi_flash_read(self, address, size) -> bytes:
        assert size <= 0x1d
        argument = address.to_bytes(4, "little") + size.to_bytes(1, "little")
        ack, report = self._send_subcmd_get_response(b'\x10', argument)
        if not ack:
            raise IOError("After SPI read @ {address:#06x}: got NACK")

        if report[:2] != b'\x90\x10':
            raise IOError("Something else than the expected ACK was recieved!")
        assert report[2:7] == argument, (report[2:5], argument)

        return report[7:size+7]

    def _update_input_report(self):  # daemon thread
        try:
            while self._joycon_device:
                report = self._read_input_report()
                # TODO, handle input reports of type 0x21 and 0x3f
                while report[0] != 0x30:
                    report = self._read_input_report()

                self._input_report = report

                for callback in self._input_hooks:
                    callback(self)
        except OSError:
            print('connection closed')
            pass

    def _read_joycon_data(self):
        color_data = self._spi_flash_read(0x6050, 6)

        self._read_stick_calibration_data()

        buf = self._spi_flash_read(0x6086 if self.is_left() else 0x6098, 16)
        self.deadzone = (buf[4] << 8) & 0xF00 | buf[3]

        # user IME data
        if self._spi_flash_read(0x8026, 2) == b"\xB2\xA1":
            # print(f"Calibrate {self.serial} IME with user data")
            imu_cal = self._spi_flash_read(0x8028, 24)

        # factory IME data
        else:
            # print(f"Calibrate {self.serial} IME with factory data")
            imu_cal = self._spi_flash_read(0x6020, 24)

        self.color_body = tuple(color_data[:3])
        self.color_btn  = tuple(color_data[3:])

        self.set_accel_calibration((
                self._to_int16le_from_2bytes(imu_cal[ 0], imu_cal[ 1]),
                self._to_int16le_from_2bytes(imu_cal[ 2], imu_cal[ 3]),
                self._to_int16le_from_2bytes(imu_cal[ 4], imu_cal[ 5]),
            ), (
                self._to_int16le_from_2bytes(imu_cal[ 6], imu_cal[ 7]),
                self._to_int16le_from_2bytes(imu_cal[ 8], imu_cal[ 9]),
                self._to_int16le_from_2bytes(imu_cal[10], imu_cal[11]),
            )
        )

    def _read_stick_calibration_data(self):
        user_stick_cal_addr = 0x8012 if self.is_left() else 0x801D
        buf = self._spi_flash_read(user_stick_cal_addr, 9)
        use_user_data = False

        for b in buf:
            if b != 0xFF:
                use_user_data = True
                break

        if not use_user_data:
            factory_stick_cal_addr = 0x603D if self.is_left() else 0x6046
            buf = self._spi_flash_read(factory_stick_cal_addr, 9)

        self.stick_cal = [0] * 6

        # X Axis Max above center
        self.stick_cal[0 if self.is_left() else 2] = (buf[1] << 8) & 0xF00 | buf[0]
        # Y Axis Max above center
        self.stick_cal[1 if self.is_left() else 3] = (buf[2] << 4) | (buf[1] >> 4)
        # X Axis Center
        self.stick_cal[2 if self.is_left() else 4] = (buf[4] << 8) & 0xF00 | buf[3]
        # Y Axis Center
        self.stick_cal[3 if self.is_left() else 5] = (buf[5] << 4) | (buf[4] >> 4)
        # X Axis Min below center
        self.stick_cal[4 if self.is_left() else 0] = (buf[7] << 8) & 0xF00 | buf[6]
        # Y Axis Min below center
        self.stick_cal[5 if self.is_left() else 1] = (buf[8] << 4) | (buf[7] >> 4)

    def _setup_sensors(self):
        # Enable 6 axis sensors
        self._write_output_report(b'\x01', b'\x40', b'\x01')
        # It needs delta time to update the setting
        time.sleep(0.02)
        # Change format of input report
        self._write_output_report(b'\x01', b'\x03', b'\x30')

    @staticmethod
    def _to_int16le_from_2bytes(hbytebe, lbytebe):
        uint16le = (lbytebe << 8) | hbytebe
        int16le = uint16le if uint16le < 32768 else uint16le - 65536
        return int16le

    def _get_nbit_from_input_report(self, offset_byte, offset_bit, nbit):
        byte = self._input_report[offset_byte]
        return (byte >> offset_bit) & ((1 << nbit) - 1)

    def __del__(self):
        self._close()

    def set_accel_calibration(self, offset_xyz=None, coeff_xyz=None):
        if offset_xyz and coeff_xyz:
            self._ACCEL_OFFSET_X, \
            self._ACCEL_OFFSET_Y, \
            self._ACCEL_OFFSET_Z = offset_xyz

            cx, cy, cz = coeff_xyz
            self._ACCEL_COEFF_X = (1.0 / (cx - self._ACCEL_OFFSET_X)) * 4.0
            self._ACCEL_COEFF_Y = (1.0 / (cy - self._ACCEL_OFFSET_Y)) * 4.0
            self._ACCEL_COEFF_Z = (1.0 / (cz - self._ACCEL_OFFSET_Z)) * 4.0


    def get_actual_stick_value(self, pre_cal, orientation):  # X/Horizontal = 0, Y/Vertical = 1
        diff = pre_cal - self.stick_cal[2 + orientation]
        if (abs(diff) < self.deadzone):
            return 0
        elif diff > 0:  # Axis is above center
            return diff / self.stick_cal[orientation]
        else:
            return diff / self.stick_cal[4 + orientation]

    def register_update_hook(self, callback):
        self._input_hooks.append(callback)
        return callback  # this makes it so you could use it as a decorator

    def is_left(self):
        return self.product_id == JOYCON_L_PRODUCT_ID

    def is_right(self):
        return self.product_id == JOYCON_R_PRODUCT_ID

    def get_battery_charging(self):
        return self._get_nbit_from_input_report(2, 4, 1)

    def get_battery_level(self):
        return self._get_nbit_from_input_report(2, 5, 3)

    def get_button_y(self):
        return self._get_nbit_from_input_report(3, 0, 1)

    def get_button_x(self):
        return self._get_nbit_from_input_report(3, 1, 1)

    def get_button_b(self):
        return self._get_nbit_from_input_report(3, 2, 1)

    def get_button_a(self):
        return self._get_nbit_from_input_report(3, 3, 1)

    def get_button_right_sr(self):
        return self._get_nbit_from_input_report(3, 4, 1)

    def get_button_right_sl(self):
        return self._get_nbit_from_input_report(3, 5, 1)

    def get_button_r(self):
        return self._get_nbit_from_input_report(3, 6, 1)

    def get_button_zr(self):
        return self._get_nbit_from_input_report(3, 7, 1)

    def get_button_minus(self):
        return self._get_nbit_from_input_report(4, 0, 1)

    def get_button_plus(self):
        return self._get_nbit_from_input_report(4, 1, 1)

    def get_button_r_stick(self):
        return self._get_nbit_from_input_report(4, 2, 1)

    def get_button_l_stick(self):
        return self._get_nbit_from_input_report(4, 3, 1)

    def get_button_home(self):
        return self._get_nbit_from_input_report(4, 4, 1)

    def get_button_capture(self):
        return self._get_nbit_from_input_report(4, 5, 1)

    def get_button_charging_grip(self):
        return self._get_nbit_from_input_report(4, 7, 1)

    def get_button_down(self):
        return self._get_nbit_from_input_report(5, 0, 1)

    def get_button_up(self):
        return self._get_nbit_from_input_report(5, 1, 1)

    def get_button_right(self):
        return self._get_nbit_from_input_report(5, 2, 1)

    def get_button_left(self):
        return self._get_nbit_from_input_report(5, 3, 1)

    def get_button_left_sr(self):
        return self._get_nbit_from_input_report(5, 4, 1)

    def get_button_left_sl(self):
        return self._get_nbit_from_input_report(5, 5, 1)

    def get_button_l(self):
        return self._get_nbit_from_input_report(5, 6, 1)

    def get_button_zl(self):
        return self._get_nbit_from_input_report(5, 7, 1)

    def get_stick_left_horizontal(self):
        if not self.is_left():
            return 0

        pre_cal = self._get_nbit_from_input_report(6, 0, 8) \
            | (self._get_nbit_from_input_report(7, 0, 4) << 8)
        return self.get_actual_stick_value(pre_cal, 0)

    def get_stick_left_vertical(self):
        if not self.is_left():
            return 0

        pre_cal = self._get_nbit_from_input_report(7, 4, 4) \
            | (self._get_nbit_from_input_report(8, 0, 8) << 4)
        return self.get_actual_stick_value(pre_cal, 1)

    def get_stick_right_horizontal(self):
        if self.is_left():
            return 0

        pre_cal = self._get_nbit_from_input_report(9, 0, 8) \
            | (self._get_nbit_from_input_report(10, 0, 4) << 8)
        return self.get_actual_stick_value(pre_cal, 0)

    def get_stick_right_vertical(self):
        if self.is_left():
            return 0

        pre_cal = self._get_nbit_from_input_report(10, 4, 4) \
            | (self._get_nbit_from_input_report(11, 0, 8) << 4)
        return self.get_actual_stick_value(pre_cal, 1)

    def get_accels(self):
        input_report = bytes(self._input_report)
        accels = []

        for idx in range(3):
            x = self.get_accel_x(input_report, sample_idx=idx)
            y = self.get_accel_y(input_report, sample_idx=idx)
            z = self.get_accel_z(input_report, sample_idx=idx)
            accels.append((x, y, z))

        return accels

    def get_accel_x(self, input_report=None, sample_idx=0):
        if not input_report:
            input_report = self._input_report

        if sample_idx not in (0, 1, 2):
            raise IndexError('sample_idx should be between 0 and 2')
        data = self._to_int16le_from_2bytes(
            input_report[13 + sample_idx * 12],
            input_report[14 + sample_idx * 12])
        return data * self._ACCEL_COEFF_X

    def get_accel_y(self, input_report=None, sample_idx=0):
        if not input_report:
            input_report = self._input_report

        if sample_idx not in (0, 1, 2):
            raise IndexError('sample_idx should be between 0 and 2')
        data = self._to_int16le_from_2bytes(
            input_report[15 + sample_idx * 12],
            input_report[16 + sample_idx * 12])
        return data * self._ACCEL_COEFF_Y * (1 if self.is_left() else -1)

    def get_accel_z(self, input_report=None, sample_idx=0):
        if not input_report:
            input_report = self._input_report

        if sample_idx not in (0, 1, 2):
            raise IndexError('sample_idx should be between 0 and 2')
        data = self._to_int16le_from_2bytes(
            input_report[17 + sample_idx * 12],
            input_report[18 + sample_idx * 12])
        return data * self._ACCEL_COEFF_Z * (1 if self.is_left() else -1)

    def get_status(self) -> dict:
        return {
            "battery": {
                "charging": self.get_battery_charging(),
                "level": self.get_battery_level(),
            },
            "buttons": {
                "right": {
                    "y": self.get_button_y(),
                    "x": self.get_button_x(),
                    "b": self.get_button_b(),
                    "a": self.get_button_a(),
                    "sr": self.get_button_right_sr(),
                    "sl": self.get_button_right_sl(),
                    "r": self.get_button_r(),
                    "zr": self.get_button_zr(),
                },
                "shared": {
                    "minus": self.get_button_minus(),
                    "plus": self.get_button_plus(),
                    "r-stick": self.get_button_r_stick(),
                    "l-stick": self.get_button_l_stick(),
                    "home": self.get_button_home(),
                    "capture": self.get_button_capture(),
                    "charging-grip": self.get_button_charging_grip(),
                },
                "left": {
                    "down": self.get_button_down(),
                    "up": self.get_button_up(),
                    "right": self.get_button_right(),
                    "left": self.get_button_left(),
                    "sr": self.get_button_left_sr(),
                    "sl": self.get_button_left_sl(),
                    "l": self.get_button_l(),
                    "zl": self.get_button_zl(),
                }
            },
            "analog-sticks": {
                "left": {
                    "horizontal": self.get_stick_left_horizontal(),
                    "vertical": self.get_stick_left_vertical(),
                },
                "right": {
                    "horizontal": self.get_stick_right_horizontal(),
                    "vertical": self.get_stick_right_vertical(),
                },
            },
            "accel": self.get_accels(),
        }

    def disconnect_device(self):
        self._write_output_report(b'\x01', b'\x06', b'\x00')


if __name__ == '__main__':
    import pyjoycon.device as d
    ids = d.get_L_id() if None not in d.get_L_id() else d.get_R_id()

    if None not in ids:
        joycon = JoyCon(*ids)
        lamp_pattern = 0
        while True:
            print(joycon.get_status())
            joycon.set_player_lamp_on(lamp_pattern)
            lamp_pattern = (lamp_pattern + 1) & 0xf
            time.sleep(0.2)
