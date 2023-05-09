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

# Interesting characteristics
Char_weight = '00008a21-0000-1000-8000-00805f9b34fb'  # weight data
Char_body = '00008a22-0000-1000-8000-00805f9b34fb'  # body data
Char_command = '00008a81-0000-1000-8000-00805f9b34fb'  # command register
Char_person = '00008a82-0000-1000-8000-00805f9b34fb'  # person data

'''
The decode functions Read Medisana BS440 Scale hex Indication and 
decodes scale values from hex data string.
Each function receives the hex handle and bytevalues and
return a dictionary with the decoded data
'''

def decodePerson(handle, values):
    '''
    decodePerson
    handle: 0x25
    values[0] = 0x84
    Returns a dict for convenience:
        valid (True, False)
        person (1..9)
        gender (male|female)
        age (0..255 years)
        size (0..255 cm)
        activity (normal|high)
    '''
    data = unpack('BxBxBBBxB', bytes(values[0:9]))
    retDict = {}
    retDict["valid"] = (data[0] == 0x84)
    retDict["person"] = data[1]
    if data[2] == 1:
        retDict["gender"] = "male"
    else:
        retDict["gender"] = "female"
    retDict["age"] = data[3]
    retDict["size"] = data[4]
    if data[5] == 3:
        retDict["activity"] = "high"
    else:
        retDict["activity"] = "normal"
    return retDict

def sanitize_timestamp(timestamp):

    retTS = time.time()

    return retTS

def decodeWeight(handle, values):
    '''
    decodeWeight
    Handle: 0x1b
    Byte[0] = 0x1d
    Returns:
        valid (True, False)
        weight (5,0 .. 180,0 kg)
        timestamp (unix timestamp date and time of measurement)
        person (1..9)
        note: in python 2.7 to force results to be floats,
        devide by float.
        '''
    data = unpack('<BHxxIxxxxB', bytes(values[0:14]))
    retDict = {}
    retDict["valid"] = (data[0] == 0x1d)
    retDict["weight"] = data[1]/100.0
    retDict["timestamp"] = sanitize_timestamp(data[2])
    retDict["person"] = data[3]
    return retDict

def decodeBody(handle, values):
    '''
    decodeBody
    Handle: 0x1e
    Byte[0] = 0x6f
    Returns:
        valid (True, False)
        timestamp (unix timestamp date and time of measurement)
        person (1..9)
        kcal = (0..65025 Kcal)
        fat = (0..100,0 %)  percentage of body fat
        tbw = (0..100,0 %) percentage of water
        muscle = (0..100,0 %) percentage of muscle
        bone = (0..100,0) bone weight
        note: in python 2.7 to force results to be floats: devide by float.
    '''
    data = unpack('<BIBHHHHH', bytes(values[0:16]))
    retDict = {}
    retDict["valid"] = (data[0] == 0x6f)
    retDict["timestamp"] = sanitize_timestamp(data[1])
    retDict["person"] = data[2]
    retDict["kcal"] = data[3]
    retDict["fat"] = (0x0fff & data[4])/10.0
    retDict["tbw"] = (0x0fff & data[5])/10.0
    retDict["muscle"] = (0x0fff & data[6])/10.0
    retDict["bone"] = (0x0fff & data[7])/10.0
    return retDict

def processIndication(handle, values):
    '''
    Indication handler:
    Receives indication and stores values into result Dict
    (see decode functions for Dict definition)
    handle: byte
    value: bytearray
    '''
    if handle == handle_person:
        result = decodePerson(handle, values)
        if result not in persondata:
            log.info(str(result))
            persondata.append(result)
        else:
            log.info('Duplicate persondata record')
    elif handle == handle_weight:
        result = decodeWeight(handle, values)
        if result not in weightdata:
            log.info(str(result))
            weightdata.append(result)
        else:
            log.info('Duplicate weightdata record')
    elif handle == handle_body:
        result = decodeBody(handle, values)
        if result not in bodydata:
            log.info(str(result))
            bodydata.append(result)
        else:
            log.info('Duplicate bodydata record')
    else:
        log.debug('Unhandled Indication encountered')

def wait_for_device(devname):
    global adapter
    found = False
    while not found:
        try:
            # wait for scale to wake up and connect to it
            found = adapter.filtered_scan(devname)
        except pygatt.exceptions.BLEError:
            # reset adapter when (see issue #33)
            adapter.reset()
    return


def connect_device(address):
    device_connected = False
    tries = 3
    device = None
    while not device_connected and tries > 0:
        try:
            device = adapter.connect(address, 8, addresstype)
            device_connected = True
        except pygatt.exceptions.NotConnectedError:
            tries -= 1
    return device


def init_ble_mode():
    global log
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

config = ConfigParser()
config.read('BS440.ini')
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

ble_address = config.get('Scale', 'ble_address')
device_name = config.get('Scale', 'device_name')
device_model = config.get('Scale', 'device_model')

if device_model == 'BS410':
    addresstype = pygatt.BLEAddressType.public
    # On BS410 time=0 equals 1/1/2010. 
    # time_offset is used to convert to unix standard
    time_offset = 1262304000
else:
    addresstype = pygatt.BLEAddressType.random
    time_offset = 0

'''
Start BLE comms and run that forever
'''
log.info('BS440 Started')
if not init_ble_mode():
    sys.exit()

adapter = pygatt.backends.GATTToolBackend()
adapter.start()

while True:
    wait_for_device(device_name)
    device = connect_device(ble_address)
    if device:
        persondata = []
        weightdata = []
        bodydata = []

        handle_body = device.get_handle(Char_body)
        handle_person = device.get_handle(Char_person)
        handle_weight = device.get_handle(Char_weight)

        for _ in range(0, 9):
            values = read_device(device, handle_person)
            person = decodePerson(handle_person, values)
            persondata.append(person)
            log.debug("Person %d %s" % (person['person'], person))

        for _ in range(0, 20):
            values = read_device(device, handle_weight)
            weight = decodeWeight(handle_weight, values)
            weightdata.append(weight)
            log.debug("Weight %d %s" % (weight['person'], weight))

        for _ in range(0, 20):
            values = read_device(device, handle_body)
            body = decodeBody(handle_body, values)
            bodydata.append(body)
            log.debug("Body %d %s" % (body['person'], body))

        device.disconnect()

        # Call the plugins
        for plugin_name, plugin in plugins.items():
            log.info("Calling plugin %s" % plugin_name)
            plugin.execute(persondata, weightdata, bodydata)

    else:
        log.error("Cannot connect to scale")

adapter.stop()
