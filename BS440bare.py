import multiprocessing
import sys
import pygatt.backends
import logging
from configparser import ConfigParser
import time
import subprocess
from struct import *
from binascii import hexlify
import os
import threading
from time import sleep
import RPi.GPIO as GPIO
from mfrc522 import SimpleMFRC522

# Relevant characteristics submitted by the scale
# (Explanation see below)
Char_weight = '00008a21-0000-1000-8000-00805f9b34fb'  # weight data
Char_command = '00008a81-0000-1000-8000-00805f9b34fb'  # command register

def decodeWeight(handle, values):
    data = unpack('<BHxxIxxxxB', bytes(values[0:14]))
    retDict = {}
    retDict["valid"] = (data[0] == 0x1d)
    # Weight is reported in 10g. Hence, divide by 100.0
    # To force results to be floats: devide by float.
    retDict["weight"] = data[1] / 100.0
    retDict["timestamp"] = sanitize_timestamp(data[2])
    retDict["person"] = data[3]
    return retDict

def sanitize_timestamp(timestamp):
    retTS = 0
    # Fail-safe: The timestamp will only be sanitized, if it will be
    # below below the maximum unix timestamp (2147483647). Otherwise the
    # non-sanitized timestamp will be taken.
    if timestamp + time_offset < sys.maxsize:
        retTS = timestamp + time_offset
    else:
        retTS = timestamp
    # If already the non-sanitized timestamp is above the maximum unix timestamp
    # 0 will be taken instead.
    if timestamp >= sys.maxsize:
        retTS = 0
    return retTS

def processIndication(handle, values):
    if handle == handle_weight:
        result = decodeWeight(handle, values)
        if result not in weightdata:
            log.info(str(result))
            weightdata.append(result)
        else:
            log.info('Duplicate weightdata record')
    else:
        log.debug('Unhandled Indication encountered')

def wait_for_device(devname):
    '''
    Reset adapter in case of pygatt exception error
    '''
    found = False
    while not found:
        try:
            # wait for scale to wake up and connect to it under the scale name (devname)
            found = adapter.filtered_scan(devname)
        except pygatt.exceptions.BLEError:
            # reset adapter when (see issue /keptenkurk/BS440/issues/33)
            adapter.reset()
    return

def connect_device(address):
    '''
    Connects to the scale defined by the MAC address (address).
    If successful, returns the instance of the BLEDevice. Otherwise NULL.
    '''
    device_connected = False
    tries = 3
    device = None
    while not device_connected and tries > 0:
        try:
            # address: MAC address of the scale
            # 8: ?
            # addresstype: ?
            device = adapter.connect(address, 8, addresstype)
            device_connected = True
        except pygatt.exceptions.NotConnectedError:
            tries -= 1
    return device

def init_ble_mode():
    '''
    Activates Bluetooth LE
    '''
    p = subprocess.Popen("sudo btmgmt le on", stdout=subprocess.PIPE,
                         shell=True)
    (output, err) = p.communicate()
    if not err:
        log.info(output)
        return True
    else:
        log.info(err)
        return False

'''
Main program loop
'''
# Read .ini file and set plugins-folder
config = ConfigParser()
config.read('BS430.ini')
path = "plugins/"
plugins = {}

# set up logging
numeric_level = getattr(logging,
                        config.get('Program', 'loglevel').upper(),
                        None)
if not isinstance(numeric_level, int):
    raise ValueError('Invalid log level: %s' % loglevel)
logging.basicConfig(level=numeric_level,
                    format='%(asctime)s %(levelname)-8s %(funcName)s %(message)s',
                    datefmt='%a, %d %b %Y %H:%M:%S',
                    filename=config.get('Program', 'logfile'),
                    filemode='w')
log = logging.getLogger(__name__)
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(numeric_level)
formatter = logging.Formatter('%(asctime)s %(levelname)-8s %(funcName)s %(message)s')
ch.setFormatter(formatter)
log.addHandler(ch)

# Load configured plugins
if config.has_option('Program', 'plugins'):
    config_plugins = config.get('Program', 'plugins').split(',')
    config_plugins = [plugin.strip(' ') for plugin in config_plugins]
    log.info('Configured plugins: %s' % ', '.join(config_plugins))

    sys.path.insert(0, path)
    for plugin in config_plugins:
        log.info('Loading plugin: %s' % plugin)
        mod = __import__(plugin)
        plugins[plugin] = mod.Plugin()
    log.info('All plugins loaded.')
else:
    log.info('No plugins configured.')
sys.path.pop(0)

# Load scale information from .ini-file
ble_address = config.get('Scale', 'ble_address')
device_name = config.get('Scale', 'device_name')
device_model = config.get('Scale', 'device_model')

# Set BLE address type and time offset, depending on scale model
if device_model == 'BS410':
    addresstype = pygatt.BLEAddressType.public
    # On BS410 time=0 equals 1/1/2010.
    # time_offset is used to convert to unix standard
    time_offset = 1262304000
elif device_model == 'BS444':
    addresstype = pygatt.BLEAddressType.public
    # On BS444 time=0 equals 1/1/2010.
    # time_offset is used to convert to unix standard
    time_offset = 1262304000
else:
    addresstype = pygatt.BLEAddressType.random
    time_offset = 0

'''
Start BLE comms and run that forever
'''
log.info('BS430 Started')
if not init_ble_mode():
    sys.exit()

adapter = pygatt.backends.GATTToolBackend()
adapter.start()

while True:
    wait_for_device(device_name)
    device = connect_device(ble_address)
    # If the device was connected successfully (the variable "device" has
    # been defined and contains the instance of the BLEDevice) the main loop runs
    if device:
        weightdata = []
        try:
            # Get the two-byte shortcut (the handle)
            handle_weight = device.get_handle(Char_weight)
            handle_command = device.get_handle(Char_command)
            continue_comms = True
        except pygatt.exceptions.NotConnectedError:
            log.warning('Error getting handles')
            continue_comms = False

        log.info('Continue Comms: ' + str(continue_comms))
        if not continue_comms:
            continue

        # Subscribe to characteristics and have processIndication
        # process the data received.
        try:
            device.subscribe(Char_weight,
                             callback=processIndication,
                             indication=True)
        except pygatt.exceptions.NotConnectedError:
            continue_comms = False

        # Send the unix timestamp in little endian order preceded by 02 as
        # bytearray to handle 0x23. This will resync the scale's RTC.
        if continue_comms:
            timestamp = bytearray(pack('<I', int(time.time() - time_offset)))
            timestamp.insert(0, 2)
            try:
                device.char_write_handle(handle_command, timestamp, wait_for_response=True)
            except pygatt.exceptions.NotificationTimeout:
                pass
            except pygatt.exceptions.NotConnectedError:
                continue_comms = False

            if continue_comms:
                log.info('Waiting for notifications for another 30 seconds')
                time.sleep(30)
                try:
                    device.disconnect()
                except pygatt.exceptions.NotConnectedError:
                    log.info('Could not disconnect...')

                log.info('Done receiving data from scale')

                # Process data if all received well
                if weightdata:
                    # Sort scale output by timestamp to retrieve most recent three results
                    weightdatasorted = sorted(weightdata, key=lambda k: k['timestamp'], reverse=True)

                    # Run all plugins found
                    for plugin in plugins.values():
                        plugin.execute(config, weightdatasorted)
                else:
                    log.error('Unreliable data received. Unable to process')
