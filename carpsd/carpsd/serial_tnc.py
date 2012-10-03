#!/usr/bin/python

# Copyright 2012 Carpcomm GmbH
# Author: Timothy Stranex <tstranex@carpcomm.com>

"""Controller for KISS TNCs connected via a serial device."""

import upload

import serial
import threading
import logging
import time


SERIAL_READ_TIMEOUT = 0.5


class KISSDecoder(object):
    """State machine that extracts KISS frames from a stream of bytes."""

    FEND = '\xc0'
    FESC = '\xdb'
    TFEND = '\xdc'
    TFESC = '\xdd'

    def __init__(self):
        self.current_frame = None
        self.state = 0
        self.frames = []

    def _WriteChar(self, c):
        # We extract KISS frames using a state machine.
        # KISS protocol documentation:
        # http://www.ka9q.net/papers/kiss.html

        if self.state == 0:
            # We're waiting for a frame to start.
            if c == self.FEND:
                self.state = 1
                self.current_frame = ''
        elif self.state == 1:
            # We're inside a frame.
            if c == self.FEND:
                if len(self.current_frame) > 1:  # Ignore empty frames.
                    data_type = self.current_frame[0]
                    if data_type != '\x00':
                        logging.info(
                            'Received unknown KISS frame data type from TNC: ' +
                            '%x', ord(data_type))
                    self.frames.append(self.current_frame[1:])
                self.state = 0
                self.current_frame = None
            elif c == self.FESC:
                self.state = 2
            else:
                self.current_frame += c
        elif self.state == 2:
            # We're in escape mode.
            if c == self.TFEND:
                self.current_frame += self.FEND
                self.state = 1
            elif c == self.TFESC:
                self.current_frame += self.FESC
                self.state = 1
            else:
                # Error, ignore it.
                self.state = 1
                logging.info(
                    'Received invalid KISS escape char from TNC: %x', ord(c))
        else:
            # Unknown state. This shouldn't happen.
            logging.error('Invalid KISS state %d. Resetting.', self.state)
            self.state = 0

    def Write(self, data):
        for c in data:
            self._WriteChar(c)

    def ReadFrames(self):
        f = self.frames
        self.frames = []
        return f


class _SerialReadThread(threading.Thread):
    """Thread that reads KISS frames from a serial device and uploads them."""

    def __init__(self, serial, api_client, satellite_id):
        threading.Thread.__init__(self)

        self.serial = serial
        self.api_client = api_client
        self.satellite_id = satellite_id

        self.decoder = KISSDecoder()
        self.should_stop = False

    def run(self):
        while not self.should_stop:
            data = self.serial.read()  # Block until there is some data.
            data += self.serial.read(self.serial.inWaiting())

            if data:
                logging.info('[debug] Recevied serial data: %s', `data`)

            self.decoder.Write(data)

            if not self.api_client:
                continue

            timestamp = int(time.time())
            frames = self.decoder.ReadFrames()
            for f in frames:
                ok, status = self.api_client.PostPacket(
                    self.satellite_id, timestamp, f)
                logging.info('[debug] Posted TNC frame: %s', `f`)
                if not ok:
                    host, port = self.api_client.GetServer()
                    logging.info('Error uploading packet to %s:%d: %d, %s',
                                 host, port, status[0], status[1])

        self.serial.close()

    def Stop(self):
        self.should_stop = True

    def GetLatestFrames(self):
        return self.decoder.ReadFrames()


class SerialTNC(object):
    """Controller for KISS TNCs connected via a serial device."""

    def __init__(self, config):
        self._device = config.get(SerialTNC.__name__, 'device')
        self._baud = int(config.get(SerialTNC.__name__, 'baud'))
        self._api_client = upload.APIClient(config)
        self._thread = None

    def _OpenSerial(self):
        # We need the timeout otherwise the read thread cannot be stopped.
        return serial.Serial(
            self._device, self._baud, timeout=SERIAL_READ_TIMEOUT)

    def Verify(self):
        """Do some quick checks to make sure the configuration works."""
        try:
            s = self._OpenSerial()
            s.close()
        except serial.SerialException, e:
            logging.error('Error opening serial port: %s', e)
            return False
        return True

    def Start(self, api_host, api_port, satellite_id):
        """Start a thread to read packets from the device and upload them.

        If api_host is set to an empty string, then packets are read but
        not uploaded."""

        if self._thread is not None:
            logging.info('Error starting TNC thread: already started')
            return False

        ac = None
        if api_host:
            self._api_client.SetServer(api_host, api_port)
            ac = self._api_client

        self._thread = _SerialReadThread(self._OpenSerial(), ac, satellite_id)
        self._thread.start()

        logging.info('Started serial TNC thread.')

        return True

    def Stop(self):
        t = self._thread
        if t is None:
            # It's already stopped.
            return True

        t.Stop()
        t.join()

        self._thread = None
        
        if t.isAlive():
            logging.info('Error stopping serial TNC thread. It is still alive.')
            return False

        logging.info('Stopped serial TNC thread.')
        return True

    def GetLatestFrames(self):
        if self._thread is not None:
            return True, self._thread.GetLatestFrames()
        else:
            return False, []


def Configure(config):
    if config.has_section(SerialTNC.__name__):
        return SerialTNC(config)
