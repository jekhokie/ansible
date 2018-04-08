#!/usr/bin/python

ANSIBLE_METADATA = {
    'metadata_version': '0.1',
    'status': ['preview'],
    'supported_by': 'community'
}

DOCUMENTATION = '''
---
module: vra_guest

short_description: Provisioning of a VMware vRA guest from a blueprint

version_added: "2.6"

description:
    - "Create a VM from a Blueprint via vRealizeAutomation (vRA)"

options:
    blueprint_name:
        description:
            - Name of the Blueprint to use for provisioning
        required: true
    cpu:
        description:
            - Number of CPUs for the VM (integer)
        required: true
    disk:
        description:
            - Array of disks to add to the VM
            - 'Valid attributes are:'
            - ' - C(size_gb) (integer): Disk storage size in specified unit.'
            - ' - C(mount_point) (str): Mount point (Linux) or drive letter (Windows).'
        required: false
    memory:
        description:
            - Amount of memory, in GB (integer)
        required: true
    hostname:
        description:
            - Hostname of the VM - note that this requires a custom "Hostname" property be added to the Blueprint
        required: true
    vra_hostname:
        description:
            - Hostname of the vRA instance to communicate with
        required: true
    vra_password:
        description:
            - Password of the user interacting with the API
        required: true
    vra_tenant:
        description:
            - Tenant name for the vRA provisioning
        required: true
    vra_username:
        description:
            - Name of the user interacting with the API
        required: true
    vsphere_infra_name:
        description:
            - Name of the infrastructure component inside which the template parameters reside for CPU, Disk, Memory, etc.
        required: true

requirements:
    - copy
    - json
    - requests
    - time

author:
    - Justin Karimi (@jekhokie) <jekhokie@gmail.com>
'''

EXAMPLES = '''
- name: Create a VM from a Blueprint
  vra_guest:
    blueprint_name: "Linux"
    cpu: 2
    disk:
        - size_gb: 60
          mount_point: "/mnt1"
        - size_gb: 80
          mount_point: "/mnt2"
    memory: 4096
    hostname: "Test-VM"
    vra_hostname: "my-vra-host.localhost"
    vra_password: "super-secret-pass"
    vra_tenant: "vsphere.local"
    vra_username: "automation-user"
    vsphere_infra_name: "vSphere__vCenter__Machine_1"
'''

RETURN = '''
instance:
    description: Metadata about the newly-created VM
    type: dict
    returned: always
    sample: none
'''

import copy
import json
import requests
import time
from ansible.module_utils.basic import AnsibleModule
from requests.packages.urllib3.exceptions import InsecureRequestWarning

# ignore annoyances
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

class VRAHelper(object):
    def __init__(self, module):
        self.module = module
        self.blueprint_name = module.params['blueprint_name']
        self.cpu = module.params['cpu']
        self.disks = module.params['disk']
        self.ip = None
        self.memory = module.params['memory']
        self.hostname = module.params['hostname']
        self.headers = {
            "accept": "application/json",
            "content-type": "application/json"
        }
        self.vra_hostname = module.params['vra_hostname']
        self.vra_password = module.params['vra_password']
        self.vra_tenant = module.params['vra_tenant']
        self.vra_username = module.params['vra_username']
        self.vsphere_infra_name = module.params['vsphere_infra_name']

        # initialize bearer token for auth
        self.get_auth()

    def get_auth(self):
        try:
            url = "https://%s/identity/api/tokens" % (self.vra_hostname)
            payload = '{"username":"%s","password":"%s","tenant":"%s"}' % (self.vra_username, self.vra_password, self.vra_tenant)
            response = requests.request("POST", url, data=payload, headers=self.headers, verify=False)

            # format bearer token into correct auth pattern
            token = response.json()['id']
            self.headers["authorization"] = "Bearer %s" % token
        except Exception as e:
            self.module.fail_json(msg="Failed to get bearer token: %s" % (e))

    def get_catalog_id(self):
        catalog_dict = {}

        try:
            url = "https://%s/catalog-service/api/consumer/entitledCatalogItems" % (self.vra_hostname)
            response = requests.request("GET", url, headers=self.headers, verify=False)

            for i in response.json()['content']:
                item_name = i['catalogItem']['name']
                item_id = i['catalogItem']['id']
                catalog_dict[item_name] = item_id

            self.catalog_id = catalog_dict[self.blueprint_name]
        except Exception as e:
            self.module.fail_json(msg="Failed to get catalog ID for blueprint %s: %s" % (self.blueprint_name, e))

    def get_template_json(self):
        try:
            url = "https://%s/catalog-service/api/consumer/entitledCatalogItems/%s/requests/template" % (self.vra_hostname, self.catalog_id)
            response = requests.request("GET", url, headers=self.headers, verify=False)

            self.template_json = response
        except Exception as e:
            self.module.fail_json(msg="Failed to get template JSON for creating the VM: %s" % (e))

    def customize_template(self):
        template = dict(self.template_json.json())
        metadata = template['data'][self.vsphere_infra_name]['data']
        metadata['cpu'] = self.cpu
        metadata['memory'] = self.memory
        metadata['Hostname'] = self.hostname

        if len(self.disks) >= 1:
            disk_meta_orig = copy.deepcopy(metadata['disks'][0])
            disk_id = disk_meta_orig['data']['id']
            metadata['disks'] = []

            for i, disk in enumerate(self.disks):
                disk_id += 1
                disk_meta = copy.deepcopy(disk_meta_orig)
                disk_meta['data']['capacity'] = self.disks[i]['size_gb']
                disk_meta['data']['label'] = "Hard Disk %s" % (i+1)
                disk_meta['data']['volumeId'] = i
                disk_meta['data']['id'] = disk_id
                disk_meta['data']['initial_location'] = self.disks[i]['mount_point']
                metadata['disks'].append(disk_meta)

        self.template_json = template

    def create_vm_from_template(self):
        try:
            url = "https://%s/catalog-service/api/consumer/entitledCatalogItems/%s/requests" % (self.vra_hostname, self.catalog_id)
            response = requests.request("POST", url, headers=self.headers, data=json.dumps(self.template_json), verify=False)

            self.request_id = response.json()['id']
        except Exception as e:
            self.module.fail_json(msg="Failed to create VM from template: %s" % (e))

    def get_vm(self):
        try:
            url = "https://%s/catalog-service/api/consumer/resources/types/Infrastructure.Virtual/?limit=5000" % (self.vra_hostname)
            response = requests.request("GET", url, headers=self.headers, verify=False)

            vms = [i for i in response.json()['content'] if i['name'] == self.hostname]
            if len(vms) > 1:
                self.module.fail_json(msg="Duplicate VMs with hostname %s." % (self.hostname))
            elif len(vms) == 1:
                self.request_id = vms[0]['requestId']

                # get details about the VM
                url = "https://%s/catalog-service/api/consumer/requests/%s/resources" % (self.vra_hostname, self.request_id)
                response = requests.request("GET", url, headers=self.headers, verify=False)

                # get the Destroy ID and VM Name using list comprehension
                meta_dict = [element for element in response.json()['content'] if element['providerBinding']['providerRef']['label'] == 'Infrastructure Service'][0]
                self.destroy_id = meta_dict['id']

                # get the VM IP address using list comprehension
                vm_data = [element for element in meta_dict['resourceData']['entries'] if element['key'] == 'ip_address'][0]
                self.ip = vm_data['value']['value']
        except Exception as e:
            self.module.fail_json(msg="Failed to get VM details for '%s': %s" % (self.hostname, e))

    def get_vm_status(self):
        try:
            url = "https://%s/catalog-service/api/consumer/requests/%s" % (self.vra_hostname, self.request_id)
            response = requests.request("GET", url, headers=self.headers, verify=False)

            self.build_status = response.json()['stateName']
            explanation = response.json()['requestCompletion']
            if explanation is None:
                self.build_explanation = ""
            else:
                self.build_explanation = explanation['completionDetails']
        except Exception as e:
            self.module.fail_json(msg="Failed to get VM create status: %s" % (e))

def run_module():
    # available options for the module
    module_args = dict(
        blueprint_name=dict(type='str', required=True),
        cpu=dict(type='int', required=True),
        disk=dict(type='list', default=[]),
        memory=dict(type='int', required=True),
        hostname=dict(type='str', required=True),
        vra_hostname=dict(type='str', required=True),
        vra_password=dict(type='str', required=True, no_log=True),
        vra_tenant=dict(type='str', required=True),
        vra_username=dict(type='str', required=True),
        vsphere_infra_name=dict(type='str', required=True)
    )

    # seed result dict that is returned
    result = dict(
        changed=False,
        failed=False
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True
    )

    # if check mode only, just return result
    # TODO: Update this to be more robust (check if changes need to be made, etc.)
    if module.check_mode:
        module.exit_json(**result)

    # initialize the interface and get a bearer token
    vra_helper = VRAHelper(module)
    vra_helper.get_vm()

    result['changed'] = False
    # only create the VM if it doesn't already exist
    if vra_helper.ip == None:
        result['changed'] = True
        vra_helper.get_catalog_id()
        vra_helper.get_template_json()
        vra_helper.customize_template()
        vra_helper.create_vm_from_template()

        timer = 0
        timeout_seconds = 600
        while True:
            vra_helper.get_vm_status()

            if timer >= timeout_seconds:
                module.fail_json(msg="Failed to create VM in %s seconds: %s" % (timer, e))
            elif vra_helper.build_status == 'Failed':
                module.fail_json(msg="Failed to create VM: %s" % vra_helper.build_explanation)
            elif vra_helper.build_status == 'Successful':
                break

            time.sleep(15)
            timer += 15

        vra_helper.get_vm()

    # assign results for output
    result['destroy_id'] = vra_helper.destroy_id
    result['hostname'] = vra_helper.hostname
    result['ip'] = vra_helper.ip

    # successful run
    module.exit_json(**result)

def main():
    run_module()

if __name__ == '__main__':
    main()
