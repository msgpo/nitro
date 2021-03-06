import os
import sys
import logging
import time
import libvirt
import socket
import datetime
from functools import partial
from threading import Thread, Event
import xml.etree.ElementTree as tree

# local
sys.path.insert(1, os.path.realpath('..'))
from nitro.nitro import Nitro
from nitro.libvmi import LibvmiError
from nitro.event import SyscallDirection
from nitro.backends.backend import Backend
from cdrom import WindowsCDROM, LinuxCDROM

SNAPSHOT_BASE = 'base'

def wait_socket(port, ip_addr, opened=True, sleep=1):
    logging.info("Waiting for the monitored service on port %d to %s", port,
        "become available" if opened else "shutdown")
    prev_state = None
    while True:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        state = sock.connect_ex((ip_addr, port))
        logging.debug("Monitor state: %d", state)
        if prev_state != state:
            logging.debug("Monitor state changed to %d", state)
            prev_state = state
        if state == 0 and opened:
            logging.info("Monitored service on port %d became available", port)
            break
        elif state != 0 and not opened:
            logging.info("Monitored service on port %d went down", port)
            break
        time.sleep(sleep)

wait_winrm = partial(wait_socket, 5985)
wait_sshd = partial(wait_socket, 22)

class NitroThread(Thread):

    def __init__(self, domain, analyze=False, enter_hooks=None, exit_hooks=None, ready_event=None):
        super().__init__()
        self.domain = domain
        self.analyze_enabled = analyze
        self.enter_hooks = enter_hooks or {}
        self.exit_hooks = exit_hooks or {}
        self.stop_request = Event()
        self.total_time = None
        self.events = []
        self.ready_event = ready_event
        self.nitro = Nitro(self.domain, self.analyze_enabled)

    def run(self):
        # start timer
        start_time = datetime.datetime.now()
        if self.analyze_enabled:
            for name, callback in self.enter_hooks.items():
                self.nitro.backend.define_hook(name, callback, direction=SyscallDirection.enter)
            for name, callback in self.exit_hooks.items():
                self.nitro.backend.define_hook(name, callback, direction=SyscallDirection.exit)
        self.nitro.listener.set_traps(True)
        if self.ready_event is not None:
            self.ready_event.set() # is this really necessary
        for event in self.nitro.listen():
            if self.analyze_enabled:
                try:
                    syscall = self.nitro.backend.process_event(event)
                except LibvmiError:
                    ev_info = event.as_dict()
                else:
                    ev_info = syscall.as_dict()
            else:
                ev_info = event.as_dict()
            self.events.append(ev_info)


        # stop timer
        self.ready_event.clear()
        stop_time = datetime.datetime.now()
        self.total_time = str(stop_time - start_time)

    def stop(self):
        self.nitro.stop()
        self.join()


class VMTestHelper:
    def __init__(self, domain, wait, cdrom):
        self.domain = domain
        if self.domain.isActive():
            self.domain.destroy()
        # revert to base snapshot if present
        try:
            snap = self.domain.snapshotLookupByName(SNAPSHOT_BASE)
            logging.info('Reverting to base snapshot')
            self.domain.revertToSnapshot(snap)
        except libvirt.libvirtError:
            logging.warning('Missing snapshot "%s"', SNAPSHOT_BASE)
        # start domain
        logging.info('Testing {}'.format(self.domain.name()))
        self.domain.create()
        # wait for IP address
        self.ip = self.wait_for_ip()
        self.wait = wait
        self.sleep_amount = 1
        logging.info('IP address: %s', self.ip)
        self.wait(self.ip, True)
        self.cdrom = cdrom

    def wait_for_ip(self, network_name='default'):
        # find MAC address
        dom_elem = tree.fromstring(self.domain.XMLDesc())
        mac_addr = dom_elem.find("./devices/interface[@type='network']/mac").get('address')
        logging.debug('MAC address: {}'.format(mac_addr))
        while True:
            net = self.domain.connect().networkLookupByName(network_name)
            leases = net.DHCPLeases()
            found = [l for l in leases if l['mac'] == mac_addr]
            if found:
                return found[0]['ipaddr']
            time.sleep(1)

    def mount_cdrom(self, cdrom_path):
        logging.info('Mounting CDROM image')
        dom_elem = tree.fromstring(self.domain.XMLDesc())
        # find cdrom
        cdrom_elem = dom_elem.find("./devices/disk[@device='cdrom']")
        # find source
        source_elem = cdrom_elem.find('./source')
        if source_elem is None:
            tree.SubElement(cdrom_elem, 'source')
            source_elem = cdrom_elem.find('./source')
        source_elem.set('file', cdrom_path)
        new_xml = tree.tostring(cdrom_elem).decode('utf-8')
        result = self.domain.updateDeviceFlags(new_xml, libvirt.VIR_DOMAIN_AFFECT_LIVE)
        logging.debug("updateDeviceFlags returned %s", result)

    def run_test(self, wait=True, analyze=True, enter_hooks=None, exit_hooks=None):
        """Run the test by mounting the cdrom into the guest
        if wait is True, it will run the Nitro thread and wait for the test to terminate.
        if wait is False, it will return an Event which will be set when the test will terminate"""
        # get iso
        cdrom_iso = self.cdrom.generate_iso()
        logging.debug("ISO path: %s", cdrom_iso)
        if wait:
            # run nitro before inserting CDROM
            ready = Event()
            nitro = NitroThread(self.domain, analyze, enter_hooks, exit_hooks, ready)
            nitro.start()
            # wait for nitro to attach before mounting the CDROM
            ready.wait()
            # mount the cdrom
            # the test is executed
            self.mount_cdrom(cdrom_iso)
            # wait on monitered to be closed
            self.wait(self.ip, False, self.sleep_amount)
            # wait for nitro thread to terminate properly
            nitro.stop()
            return (nitro.events, nitro.total_time)
        else:
            # mount the cdrom
            # the test is executed
            self.mount_cdrom(cdrom_iso)
            # create threading Event
            stop_event = Event()
            self.wait_thread = WaitThread(self.wait, False, self.ip, stop_event, self.sleep_amount)
            self.wait_thread.start()
            return stop_event

    def stop(self):
        self.domain.shutdown()
        # stop domain
        while self.domain.state()[0] != libvirt.VIR_DOMAIN_SHUTOFF:
            time.sleep(1)
        self.cdrom.cleanup()

class WindowsVMTestHelper(VMTestHelper):
    def __init__(self, domain):
        super().__init__(domain, wait_winrm, WindowsCDROM())

class LinuxVMTestHelper(VMTestHelper):
    def __init__(self, domain):
        super().__init__(domain, wait_sshd, LinuxCDROM())
        # This is quite long but it marginally helps with the performance
        # problems since the VM doesn't have to deal with incoming connections
        self.sleep_amount = 120

class WaitThread(Thread):
    def __init__(self, wait, opened, ip, stop_event, sleep=1):
        super().__init__()
        self.wait = wait
        self.opened = opened
        self.ip = ip
        self.sleep_amount = sleep
        self.stop_event = stop_event

    def run(self):
        self.wait(self.ip, self.opened, self.sleep_amount)
        self.stop_event.set()
