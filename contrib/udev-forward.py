#!/usr/bin/python3

import os
import sys
import socket
import pyudev
import syslog
import threading
import docker
import pyhash
from struct import *
from ctypes import CDLL
import argparse

NETLINK_KOBJECT_UEVENT = 15
UDEV_MONITOR_UDEV  = 2
CLONE_NEWNET = 0x40000000
UDEV_MONITOR_MAGIC = 0xFEEDCAFE

device_list = []
lock = threading.Lock()

hasher = pyhash.murmur2_32()

def bloomHash(tag):
    bits = 0
    hash = hasher(tag.encode())

    bits = bits | 1 << (hash & 63)
    bits = bits | 1 << ((hash >> 6) & 63)
    bits = bits | 1 << ((hash >> 12) & 63)
    bits = bits | 1 << ((hash >> 18) & 63)

    return bits


def buildHeader(proplen, subsys, devtype, taghash):
    header_fmt = "8s8I"
    header_size = calcsize(header_fmt)
    subsys_hash = 0
    devtype_hash = 0

    if subsys:
        subsys_hash = socket.htonl(hasher(subsys.encode()))

    if devtype:
        devtype_hash = socket.htonl(hasher(devtype.encode()))

    tag_low = socket.htonl(taghash & 0xffffffff)
    tag_high = socket.htonl(taghash >> 32)

    return pack(header_fmt, b"libudev", socket.htonl(UDEV_MONITOR_MAGIC),
                header_size, header_size, proplen, subsys_hash, devtype_hash,
                tag_low, tag_high)


def BuildPacket(dev):
    subsys = dev.subsystem
    devtype = dev.device_type

    proplist = bytearray()
    for p in dev.properties:
        proppair = p + "=" + dev.properties[p]
        proplist = proplist + proppair.encode() + bytes([0])

    tag_hash = 0
    for t in dev.tags:
        tag_hash = tag_hash | bloomHash(t)

#            print(proplist)
#            print(proplist.hex())
#            print(data[header_size:].hex())

    hdr = buildHeader(len(proplist), subsys, devtype, tag_hash)

    return hdr + proplist

def errcheck(ret, func, args):
    if ret == -1:
        e = get_errno()
        raise OSError(e, os.strerror(e))

def sendMsgThread():
    libc = CDLL('libc.so.6', use_errno=True)
    libc.setns.errcheck = errcheck
    libc.setns(nsfd.fileno(), CLONE_NEWNET)
    sendfd = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW|socket.SOCK_NONBLOCK, NETLINK_KOBJECT_UEVENT)
    if options.debug:
        print(sendfd)

    while True:
        dev = None
        lock.acquire()
        if len(device_list):
            dev = device_list.pop(0)
        lock.release()

        if dev:
            if options.debug:
                print('working event {0.action}: {0.device_path}'.format(dev))
            pkt = BuildPacket(dev)

            # Older kernels (like 4.15 on Ubuntu 18.04) return ECONNREFUSED
            # to work around this we just ignore this specific error as the
            # data still is send on the socket.
            try:
                sendfd.sendto(pkt, (0, UDEV_MONITOR_UDEV))
            except ConnectionRefusedError:
                pass

def udev_event_callback(dev):
    if options.debug:
        print('background event {0.action}: {0.device_path}'.format(dev))
    lock.acquire()
    device_list.append(dev)
    lock.release()

def main():
    parser = argparse.ArgumentParser(description='USB device passthrough for docker containers', add_help=False)

    parser.add_argument("-i", "--instance", type=str, required=True,
                        help="Docker instance")

    parser.add_argument("-d", "--debug", action="store_true",
                        help="Enable Debug Loggin")

    global options
    options = parser.parse_args()

    client = docker.from_env()
    container = client.containers.get(options.instance)
    netns_file = container.attrs['NetworkSettings']['SandboxKey']

    if options.debug:
        print("DBG: Container netns file %s" % netns_file)

    global nsfd
    nsfd = open(netns_file, "r")

    threading.Thread(target=sendMsgThread).start()

    context = pyudev.Context()
    context.log_priority = syslog.LOG_DEBUG
    monitor = pyudev.Monitor.from_netlink(context)
    observer = pyudev.MonitorObserver(monitor, callback=udev_event_callback, name='monitor-observer')

    observer.start()



if __name__ == '__main__':
    main()