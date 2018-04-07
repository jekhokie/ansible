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
        required: false
    memory:
        description:
            - Amount of memory, in GB (integer)
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
        - size_gb: 80
    memory: 4096
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
        self.memory = module.params['memory']
        self.headers = {
            "accept": "application/json",
            "content-type": "application/json"
        }
        self.hostname = module.params['vra_hostname']
        self.password = module.params['vra_password']
        self.tenant = module.params['vra_tenant']
        self.username = module.params['vra_username']
        self.vsphere_infra_name = module.params['vsphere_infra_name']

        # initialize bearer token for auth
        self.get_auth()

    def get_auth(self):
        try:
            url = "https://%s/identity/api/tokens" % (self.hostname)
            payload = '{"username":"%s","password":"%s","tenant":"%s"}' % (self.username, self.password, self.tenant)
            response = requests.request("POST", url, data=payload, headers=self.headers, verify=False)

            # format bearer token into correct auth pattern
            token = response.json()['id']
            self.headers["authorization"] = "Bearer %s" % token
        except Exception as e:
            self.module.fail_json(msg="Failed to get bearer token: %s" % (e))

    def get_catalog_id(self):
        catalog_dict = {}

        try:
            url = "https://%s/catalog-service/api/consumer/entitledCatalogItems" % (self.hostname)
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
            url = "https://%s/catalog-service/api/consumer/entitledCatalogItems/%s/requests/template" % (self.hostname, self.catalog_id)
            response = requests.request("GET", url, headers=self.headers, verify=False)

            self.template_json = response
        except Exception as e:
            self.module.fail_json(msg="Failed to get template JSON for creating the VM: %s" % (e))

    def customize_template(self):
        template = dict(self.template_json.json())
        metadata = template['data'][self.vsphere_infra_name]['data']
        metadata['cpu'] = self.cpu
        metadata['memory'] = self.memory


        if len(self.disks) == 1:
            metadata['disks'][0]['data']['capacity'] = self.disks[0]['size_gb']
        elif len(self.disks) > 1:
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
                metadata['disks'].append(disk_meta)

        self.template_json = template

    def create_vm_from_template(self):
        try:
            url = "https://%s/catalog-service/api/consumer/entitledCatalogItems/%s/requests" % (self.hostname, self.catalog_id)
            response = requests.request("POST", url, headers=self.headers, data=json.dumps(self.template_json), verify=False)

            self.request_id = response.json()['id']
        except Exception as e:
            self.module.fail_json(msg="Failed to create VM from template: %s" % (e))

    def get_vm_status(self):
        try:
            url = "https://%s/catalog-service/api/consumer/requests/%s" % (self.hostname, self.request_id)
            response = requests.request("GET", url, headers=self.headers, verify=False)

            self.build_status = response.json()['stateName']
            explanation = response.json()['requestCompletion']
            if explanation is None:
                self.build_explanation = ""
            else:
                self.build_explanation = explanation['completionDetails']
        except Exception as e:
            self.module.fail_json(msg="Failed to get VM create status: %s" % (e))

    def get_vm_details(self):
        try:
            url = "https://%s/catalog-service/api/consumer/requests/%s/resources" % (self.hostname, self.request_id)
            response = requests.request("GET", url, headers=self.headers, verify=False)

            # get the Destroy ID and VM Name using list comprehension
            meta_dict = [element for element in response.json()['content'] if element['providerBinding']['providerRef']['label'] == 'Infrastructure Service'][0]
            self.destroy_id = meta_dict['id']
            self.vm_name = meta_dict['name']

            # get the VM IP address using list comprehension
            vm_data = [element for element in meta_dict['resourceData']['entries'] if element['key'] == 'ip_address'][0]
            self.vm_ip = vm_data['value']['value']
        except Exception as e:
            self.module.fail_json(msg="Failed to get VM details: %s" % (e))

def run_module():
    # available options for the module
    module_args = dict(
        blueprint_name=dict(type='str', required=True),
        cpu=dict(type='int', required=True),
        disk=dict(type='list', default=[]),
        memory=dict(type='int', required=True),
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
    # TODO: Update this to be more robust (check if changes need to be made, etc.
    if module.check_mode:
        return result

    # update to reflect change
    # TODO: Make this meaningful
    result['changed'] = True

    # initialize the interface and get a bearer token
    vra_helper = VRAHelper(module)
    vra_helper.get_catalog_id()
    vra_helper.get_template_json()
    vra_helper.customize_template()

    # TODO: check if the VM already exists
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

    vra_helper.get_vm_details()
    result['destroy_id'] = vra_helper.destroy_id
    result['vm_name'] = vra_helper.vm_name
    result['vm_ip'] = vra_helper.vm_ip

    # successful run
    module.exit_json(**result)

def main():
    run_module()

if __name__ == '__main__':
    main()
