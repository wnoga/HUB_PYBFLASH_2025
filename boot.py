# boot.py -- run on boot-up
# can run arbitrary Python, but best to keep it minimal

import machine
import pyb
pyb.country('US')  # ISO 3166-1 Alpha-2 code, eg US, GB, DE, AU

pyb.usb_mode(
    'VCP+MSC',  # or 'MSC' only if no serial needed
    msc=(pyb.Flash(), pyb.SDCard())
)
