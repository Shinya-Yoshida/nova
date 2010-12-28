# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2010 Citrix Systems, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Helper methods for operations related to the management of VM records and
their attributes like VDIs, VIFs, as well as their lookup functions.
"""

import logging
import urllib
from xml.dom import minidom

from nova import exception
from nova import flags
from nova.auth.manager import AuthManager
from nova.compute import instance_types
from nova.compute import power_state
from nova.virt import images
from nova.virt.xenapi import HelperBase
from nova.virt.xenapi.volume_utils import StorageError


FLAGS = flags.FLAGS

XENAPI_POWER_STATE = {
    'Halted': power_state.SHUTDOWN,
    'Running': power_state.RUNNING,
    'Paused': power_state.PAUSED,
    'Suspended': power_state.SUSPENDED,
    'Crashed': power_state.CRASHED}


class ImageType:
        """
        Enumeration class for distinguishing different image types
            0 - kernel/ramdisk image (goes on dom0's filesystem)
            1 - disk image (local SR, partitioned by objectstore plugin)
            2 - raw disk image (local SR, NOT partitioned by plugin)
        """

        KERNEL_RAMDISK = 0
        DISK = 1
        DISK_RAW = 2


class VMHelper(HelperBase):
    """
    The class that wraps the helper methods together.
    """

    @classmethod
    def create_vm(cls, session, instance, kernel, ramdisk, pv_kernel=False):
        """Create a VM record.  Returns a Deferred that gives the new
        VM reference.
        the pv_kernel flag indicates whether the guest is HVM or PV
        """

        instance_type = instance_types.INSTANCE_TYPES[instance.instance_type]
        mem = str(long(instance_type['memory_mb']) * 1024 * 1024)
        vcpus = str(instance_type['vcpus'])
        rec = {
            'name_label': instance.name,
            'name_description': '',
            'is_a_template': False,
            'memory_static_min': '0',
            'memory_static_max': mem,
            'memory_dynamic_min': mem,
            'memory_dynamic_max': mem,
            'VCPUs_at_startup': vcpus,
            'VCPUs_max': vcpus,
            'VCPUs_params': {},
            'actions_after_shutdown': 'destroy',
            'actions_after_reboot': 'restart',
            'actions_after_crash': 'destroy',
            'PV_bootloader': '',
            'PV_kernel': '',
            'PV_ramdisk': '',
            'PV_args': '',
            'PV_bootloader_args': '',
            'PV_legacy_args': '',
            'HVM_boot_policy': '',
            'HVM_boot_params': {},
            'platform': {},
            'PCI_bus': '',
            'recommendations': '',
            'affinity': '',
            'user_version': '0',
            'other_config': {},
            }
        #Complete VM configuration record according to the image type
        #non-raw/raw with PV kernel/raw in HVM mode
        if instance.kernel_id:
            rec['PV_bootloader'] = ''
            rec['PV_kernel'] = kernel
            rec['PV_ramdisk'] = ramdisk
            rec['PV_args'] = 'root=/dev/xvda1'
            rec['PV_bootloader_args'] = ''
            rec['PV_legacy_args'] = ''
        else:
            if pv_kernel:
                rec['PV_args'] = 'noninteractive'
                rec['PV_bootloader'] = 'pygrub'
            else:
                rec['HVM_boot_policy'] = 'BIOS order'
                rec['HVM_boot_params'] = {'order': 'dc'}
                rec['platform'] = {'acpi': 'true', 'apic': 'true',
                                   'pae': 'true', 'viridian': 'true'}
        logging.debug('Created VM %s...', instance.name)
        vm_ref = session.call_xenapi('VM.create', rec)
        logging.debug(_('Created VM %s as %s.'), instance.name, vm_ref)
        return vm_ref

    @classmethod
    def create_vbd(cls, session, vm_ref, vdi_ref, userdevice, bootable):
        """Create a VBD record.  Returns a Deferred that gives the new
        VBD reference."""
        vbd_rec = {}
        vbd_rec['VM'] = vm_ref
        vbd_rec['VDI'] = vdi_ref
        vbd_rec['userdevice'] = str(userdevice)
        vbd_rec['bootable'] = bootable
        vbd_rec['mode'] = 'RW'
        vbd_rec['type'] = 'disk'
        vbd_rec['unpluggable'] = True
        vbd_rec['empty'] = False
        vbd_rec['other_config'] = {}
        vbd_rec['qos_algorithm_type'] = ''
        vbd_rec['qos_algorithm_params'] = {}
        vbd_rec['qos_supported_algorithms'] = []
        logging.debug(_('Creating VBD for VM %s, VDI %s ... '),
                      vm_ref, vdi_ref)
        vbd_ref = session.call_xenapi('VBD.create', vbd_rec)
        logging.debug(_('Created VBD %s for VM %s, VDI %s.'), vbd_ref, vm_ref,
                      vdi_ref)
        return vbd_ref

    @classmethod
    def find_vbd_by_number(cls, session, vm_ref, number):
        """Get the VBD reference from the device number"""
        vbds = session.get_xenapi().VM.get_VBDs(vm_ref)
        if vbds:
            for vbd in vbds:
                try:
                    vbd_rec = session.get_xenapi().VBD.get_record(vbd)
                    if vbd_rec['userdevice'] == str(number):
                        return vbd
                except cls.XenAPI.Failure, exc:
                    logging.warn(exc)
        raise StorageError(_('VBD not found in instance %s') % vm_ref)

    @classmethod
    def unplug_vbd(cls, session, vbd_ref):
        """Unplug VBD from VM"""
        try:
            vbd_ref = session.call_xenapi('VBD.unplug', vbd_ref)
        except cls.XenAPI.Failure, exc:
            logging.warn(exc)
            if exc.details[0] != 'DEVICE_ALREADY_DETACHED':
                raise StorageError(_('Unable to unplug VBD %s') % vbd_ref)

    @classmethod
    def destroy_vbd(cls, session, vbd_ref):
        """Destroy VBD from host database"""
        try:
            task = session.call_xenapi('Async.VBD.destroy', vbd_ref)
            #FIXME(armando): find a solution to missing instance_id
            #with Josh Kearney
            session.wait_for_task(0, task)
        except cls.XenAPI.Failure, exc:
            logging.warn(exc)
            raise StorageError(_('Unable to destroy VBD %s') % vbd_ref)

    @classmethod
    def create_vif(cls, session, vm_ref, network_ref, mac_address):
        """Create a VIF record.  Returns a Deferred that gives the new
        VIF reference."""
        vif_rec = {}
        vif_rec['device'] = '0'
        vif_rec['network'] = network_ref
        vif_rec['VM'] = vm_ref
        vif_rec['MAC'] = mac_address
        vif_rec['MTU'] = '1500'
        vif_rec['other_config'] = {}
        vif_rec['qos_algorithm_type'] = ''
        vif_rec['qos_algorithm_params'] = {}
        logging.debug(_('Creating VIF for VM %s, network %s.'), vm_ref,
                      network_ref)
        vif_ref = session.call_xenapi('VIF.create', vif_rec)
        logging.debug(_('Created VIF %s for VM %s, network %s.'), vif_ref,
                      vm_ref, network_ref)
        return vif_ref

    @classmethod
    def fetch_image(cls, session, image, user, project, type):
        """
        type is interpreted as an ImageType instance
        """
        url = images.image_url(image)
        access = AuthManager().get_access_key(user, project)
        logging.debug("Asking xapi to fetch %s as %s", url, access)
        fn = (type != ImageType.KERNEL_RAMDISK) and 'get_vdi' or 'get_kernel'
        args = {}
        args['src_url'] = url
        args['username'] = access
        args['password'] = user.secret
        args['add_partition'] = 'false'
        args['raw'] = 'false'
        if type != ImageType.KERNEL_RAMDISK:
            args['add_partition'] = 'true'
            if type == ImageType.DISK_RAW:
                args['raw'] = 'true'
        task = session.async_call_plugin('objectstore', fn, args)
        #FIXME(armando): find a solution to missing instance_id
        #with Josh Kearney
        uuid = session.wait_for_task(0, task)
        return uuid

    @classmethod
    def lookup_image(cls, session, vdi_ref):
        logging.debug("Looking up vdi %s for PV kernel", vdi_ref)
        fn = "is_vdi_pv"
        args = {}
        args['vdi-ref'] = vdi_ref
        #TODO: Call proper function in plugin
        task = session.async_call_plugin('objectstore', fn, args)
        pv_str = session.wait_for_task(task)
        if pv_str.lower() == 'true':
            pv = True
        elif pv_str.lower() == 'false':
            pv = False
        logging.debug("PV Kernel in VDI:%d", pv)
        return pv

    @classmethod
    def lookup(cls, session, i):
        """Look the instance i up, and returns it if available"""
        vms = session.get_xenapi().VM.get_by_name_label(i)
        n = len(vms)
        if n == 0:
            return None
        elif n > 1:
            raise exception.Duplicate(_('duplicate name found: %s') % i)
        else:
            return vms[0]

    @classmethod
    def lookup_vm_vdis(cls, session, vm):
        """Look for the VDIs that are attached to the VM"""
        # Firstly we get the VBDs, then the VDIs.
        # TODO(Armando): do we leave the read-only devices?
        vbds = session.get_xenapi().VM.get_VBDs(vm)
        vdis = []
        if vbds:
            for vbd in vbds:
                try:
                    vdi = session.get_xenapi().VBD.get_VDI(vbd)
                    # Test valid VDI
                    record = session.get_xenapi().VDI.get_record(vdi)
                    logging.debug(_('VDI %s is still available'),
                                  record['uuid'])
                except cls.XenAPI.Failure, exc:
                    logging.warn(exc)
                else:
                    vdis.append(vdi)
            if len(vdis) > 0:
                return vdis
            else:
                return None

    @classmethod
    def compile_info(cls, record):
        """Fill record with VM status information"""
        logging.info(_("(VM_UTILS) xenserver vm state -> |%s|"),
                     record['power_state'])
        logging.info(_("(VM_UTILS) xenapi power_state -> |%s|"),
                     XENAPI_POWER_STATE[record['power_state']])
        return {'state': XENAPI_POWER_STATE[record['power_state']],
                'max_mem': long(record['memory_static_max']) >> 10,
                'mem': long(record['memory_dynamic_max']) >> 10,
                'num_cpu': record['VCPUs_max'],
                'cpu_time': 0}

    @classmethod
    def compile_diagnostics(cls, session, record):
        """Compile VM diagnostics data"""
        try:
            host = session.get_xenapi_host()
            host_ip = session.get_xenapi().host.get_record(host)["address"]
            diags = {}
            xml = get_rrd(host_ip, record["uuid"])
            if xml:
                rrd = minidom.parseString(xml)
                for i, node in enumerate(rrd.firstChild.childNodes):
                    # We don't want all of the extra garbage
                    if i >= 3 and i <= 11:
                        ref = node.childNodes
                        # Name and Value
                        diags[ref[0].firstChild.data] = ref[6].firstChild.data
            return diags
        except cls.XenAPI.Failure as e:
            return {"Unable to retrieve diagnostics": e}


def get_rrd(host, uuid):
    """Return the VM RRD XML as a string"""
    try:
        xml = urllib.urlopen("http://%s:%s@%s/vm_rrd?uuid=%s" % (
            FLAGS.xenapi_connection_username,
            FLAGS.xenapi_connection_password,
            host,
            uuid))
        return xml.read()
    except IOError:
        return None