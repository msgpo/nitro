import datetime
from enum import Enum


class SyscallDirection(Enum):
    """System call direction"""
    enter = 0
    exit = 1


class SyscallType(Enum):
    """System call mechanism"""
    sysenter = 0
    syscall = 1


class NitroEvent:
    """
    ``NitroEvent`` represents a low-level system event. It contains information
    about the state of the machine when the system was stopped.
    """

    __slots__ = (
        'direction',
        'type',
        'regs',
        'sregs',
        'vcpu_nb',
        'vcpu_io',
        'time',
    )

    def __init__(self, nitro_event_str, vcpu_io):
        #: Event direction. Are we entering or exiting a system call
        self.direction = SyscallDirection(nitro_event_str.direction)
        #: System call mechanism used
        self.type = SyscallType(nitro_event_str.type)
        #: Register state
        self.regs = nitro_event_str.regs
        #: Special register state
        self.sregs = nitro_event_str.sregs
        #: Handle to the VCPU where the event originated
        self.vcpu_io = vcpu_io
        #: VCPU number
        self.vcpu_nb = self.vcpu_io.vcpu_nb
        self.time = datetime.datetime.now().isoformat()

    def __str__(self):
        type_msg = self.type.name.upper()
        dir_msg = self.direction.name.upper()
        cr3 = hex(self.sregs.cr3)
        rax = hex(self.regs.rax)
        msg = 'vcpu: {} - type: {} - direction: {} - cr3: {} - rax: {}'.format(
            self.vcpu_nb, type_msg, dir_msg, cr3, rax)
        return msg

    def as_dict(self):
        """Return dict representation of the event"""
        info = {
            'vcpu': self.vcpu_nb,
            'type': self.type.name,
            'direction': self.direction.name,
            'cr3': hex(self.sregs.cr3),
            'rax': hex(self.regs.rax),
            'time': self.time
        }
        return info

    def get_register(self, register):
        """Get register value from the event"""
        try:
            value = getattr(self.regs, register)
        except AttributeError:
            raise RuntimeError('Unknown register')
        else:
            return value

    def update_register(self, register, value):
        """Change individual register's values"""
        # get latest regs, to avoid replacing EIP by value before emulation
        self.regs = self.vcpu_io.get_regs()
        # update register if possible
        try:
            setattr(self.regs, register, value)
        except AttributeError:
            raise RuntimeError('Unknown register')
        else:
            # send new register to KVM VCPU
            self.vcpu_io.set_regs(self.regs)
