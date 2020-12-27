import os
import logging
import requests
import time
from lithops.constants import COMPUTE_CLI_MSG
from lithops.util.ibm_token_manager import IBMTokenManager

from ibm_vpc import VpcV1
from ibm_cloud_sdk_core.authenticators import IAMAuthenticator
from ibm_cloud_sdk_core import ApiException
import namegenerator

logger = logging.getLogger(__name__)


class IBMVPCInstanceClient:

    def __init__(self, ibm_vpc_config):
        logger.debug("Creating IBM VPC client")
        self.name = 'ibm_vpc'
        self.config = ibm_vpc_config

        self.endpoint = self.config['endpoint']
        self.region = self.endpoint.split('//')[1].split('.')[0]

        #optional, create VM will update new instance id
        self.instance_id = self.config['instance_id']
        #optional, create VM will update new virtual ip address
        self.ip_address = self.config.get('ip_address', None)

        self.vm_create_timeout = self.config.get('vm_create_timeout', 120)

        self.instance_data = None

        self.ssh_credentials = {'username': self.config.get('ssh_user', 'root'),
                                'password': self.config.get('ssh_password', None),
                                'key_filename': self.config.get('ssh_key_filename', None)}

        self.session = requests.session()

        iam_api_key = self.config.get('iam_api_key')

        authenticator = IAMAuthenticator(iam_api_key)
        self.service = VpcV1('2020-06-02', authenticator=authenticator)
        self.service.set_service_url(self.config['endpoint'])

        token = self.config.get('token', None)
        token_expiry_time = self.config.get('token_expiry_time', None)
        api_key_type = 'IAM'
        self.iam_token_manager = IBMTokenManager(iam_api_key, api_key_type, token, token_expiry_time)

        headers = {'content-type': 'application/json'}
        default_user_agent = self.session.headers['User-Agent']
        headers['User-Agent'] = default_user_agent + ' {}'.format(self.config['user_agent'])
        self.session.headers.update(headers)

        adapter = requests.adapters.HTTPAdapter()
        self.session.mount('https://', adapter)

        msg = COMPUTE_CLI_MSG.format('IBM VPC')
        logger.info("{} - Region: {} - Host: {}".format(msg, self.region, self.ip_address))

    def _authorize_session(self):
        self.config['token'], self.config['token_expiry_time'] = self.iam_token_manager.get_token()
        self.session.headers['Authorization'] = 'Bearer ' + self.config['token']

    def get_ssh_credentials(self):
        return self.ssh_credentials

    def get_instance(self):
        url = '/'.join([self.endpoint, 'v1', 'instances', self.instance_id
                        + f'?version={self.config["version"]}&generation={self.config["generation"]}'])
        self._authorize_session()
        res = self.session.get(url)
        return res.json()

    def get_ip_address(self):
        if self.ip_address:
            return self.ip_address
        else:
            if not self.instance_data:
                self.instance_data = self.get_instance()
            network_interface_id = self.instance_data['primary_network_interface']['id']

            url = '/'.join([self.endpoint, 'v1', 'floating_ips'
                            + f'?version={self.config["version"]}&generation={self.config["generation"]}'])
            self._authorize_session()
            res = self.session.get(url)
            floating_ips_info = res.json()

            ip_address = None
            for floating_ip in floating_ips_info['floating_ips']:
                if floating_ip['target']['id'] == network_interface_id:
                    ip_address = floating_ip['address']

            if ip_address is None:
                raise Exception('Could not find the public IP address')

        return ip_address

    def create_instance_action(self, action):
        if action in ['start', 'reboot']:
            expected_status = 'running'
        elif action == 'stop':
            expected_status = 'stopped'
        else:
            msg = 'An error occurred cant create instance action \"{}\"'.format(action)
            raise Exception(msg)

        url = '/'.join([self.config['endpoint'], 'v1', 'instances', self.config['instance_id'],
                        f'actions?version={self.config["version"]}&generation={self.config["generation"]}'])
        self._authorize_session()
        res = self.session.post(url, json={'type': action})
        resp_text = res.json()

        if res.status_code != 201:
            msg = 'An error occurred creating instance action {}: {}'.format(action, resp_text['errors'])
            raise Exception(msg)

        self.instance_data = self.get_instance()
        while self.instance_data['status'] != expected_status:
            time.sleep(1)
            self.instance_data = self.get_instance()

    def _generate_name(self, r_type, job_key, call_id):
        if (job_key != None and call_id != None):
            return "lithops" + str(job_key) + "-" + str(call_id) + "-" + r_type
        return "lithops-" + namegenerator.gen() + "-" + r_type

    def _create_instance(self, job_key, call_id):
        # security_group_identity_model = {'id': 'r006-2d3cc459-bb8b-4ec6-a5fb-28e60c9f7d7b'}
        security_group_identity_model = {'id': self.config['security_group_id']}

        # subnet_identity_model = {'id': '0737-bbc80a8f-d46a-4cc6-8a5a-991daa5fc914'}
        subnet_identity_model = {'id': self.config['subnet_id']}
        
        # key_identity_model = {'id': "r006-14719c2a-80cf-4043-8018-fa22d4ce1337"}
        key_identity_model = {'id': self.config['key_id']}

        volume_prototype_instance_by_image_context_model = {
            'capacity': 100, 'iops': 10000, 'name': self._generate_name('volume', job_key, call_id), 'profile': {'name': self.config['volume_tier_name']}}#''10iops-tier'}}

        network_interface_prototype_model = {
            'name': 'eth0', 'subnet': subnet_identity_model, 'security_groups': [security_group_identity_model]}
        volume_attachment_prototype_instance_by_image = {
            'delete_volume_on_instance_delete': True,
            'name': self._generate_name('boot', job_key, call_id),
            'volume': volume_prototype_instance_by_image_context_model
        }
        instance_prototype_model = {
            'keys': [key_identity_model], 'name': self._generate_name('instance', job_key, call_id)}
        instance_prototype_model['profile'] = {'name': self.config['profile_name']}#"bx2-8x32"}

        instance_prototype_model['resource_group'] = {'id': self.config['resource_group_id']}#"8145289ddf7047ea93fd2835de391f43"}
        instance_prototype_model['vpc'] = {'id': self.config['vpc_id']}#"r006-afdd7b5d-059f-413f-a319-c0a38ef46824"}
        instance_prototype_model['image'] = {'id': self.config['image_id']}#"r006-988caa8b-7786-49c9-aea6-9553af2b1969"}
        instance_prototype_model['zone'] = {'name': self.config['zone_name']}#"us-south-3"}

        instance_prototype_model['boot_volume_attachment'] = volume_attachment_prototype_instance_by_image
        instance_prototype_model['primary_network_interface'] = network_interface_prototype_model

        response = self.service.create_instance(instance_prototype_model)
        return response.result
        
    def _create_and_attach_floating_ip(self, instance, job_key, call_id):
        floating_ip_name = self._generate_name('fip', job_key, call_id)
        #check if floating ip exists
        floating_ip = None
        try:
            floating_ip_list = self.service.list_floating_ips().get_result()['floating_ips']
            for vip in floating_ip_list:
                if vip['name'] == floating_ip_name:
                    floating_ip =  vip
        except Exception as e:
            logger.warn(e)

        try:
            if floating_ip is None:
                # allocate new floating ip
                floating_ip_prototype_model = {}
                floating_ip_prototype_model['name'] = floating_ip_name
                floating_ip_prototype_model['zone'] = {'name': self.config['zone_name']}#"us-south-3"}
                floating_ip_prototype_model['resource_group'] = {'id': self.config['resource_group_id']}#"8145289ddf7047ea93fd2835de391f43"}

                response = self.service.create_floating_ip(floating_ip_prototype_model)

                floating_ip = response.result
        except Exception as e:
            logger.warn('Failed to create floating ip {}'.format(str(e)))
            raise e

        # we need to check if floating ip is not attached already. if not, attach it to instance
        primary_ni = instance['primary_network_interface']
        if ('target' in floating_ip and floating_ip['target']['primary_ipv4_address'] == primary_ni['primary_ipv4_address'] and
            floating_ip['target']['id'] == primary_ni['id']):
            # floating ip already atteched. do nothing
            logger.debug('Floating ip {} already attached to eth0 {}'.format(floating_ip['address'],floating_ip['target']['id']))
            return floating_ip['address']

        # attach floating ip
        try:
            response = self.service.add_instance_network_interface_floating_ip(
                instance['id'], instance['network_interfaces'][0]['id'], floating_ip['id'])
        except Exception as e:
            logger.warn('Failed to attach floating ip {} to {} : '.format(str(e)))
            raise e

        return floating_ip['address']

    def _wait_instance_running(self, instance_id):
        """
        Waits until the VM instance is running
        """
        logger.debug('Waiting VM instance to become running')

        start = time.time()
        while(time.time() - start < self.vm_create_timeout):
            instance = self.service.get_instance(instance_id).result
            if instance['status'] == 'running':
                return True
            time.sleep(1)

        self.stop()
        raise Exception('VM create failed, check logs and configurations')

    def _delete_instance(self):
        # delete floating ip
        response = self.service.list_instance_network_interfaces(self.config['instance_id'])
        for nic in response.result['network_interfaces']:
            if 'floating_ips' in nic:
                for fip in nic['floating_ips']:
                    self.service.delete_floating_ip(fip['id'])
                    print("floating ip {} been deleted".format(fip['address']))

        # delete vm instance
        self.service.delete_instance(self.config['instance_id'])
        print("instance {} been deleted".format(self.config['instance_id']))

    def start(self):
        logger.info("Starting VM instance")
        self.create_instance_action('start')
        logger.debug("VM instance started successfully")

    def create(self, job_key = None, call_id = None):
        logger.info("Creating VM instance")
        self.instance_id = None

        #check if VSI exists
        all_instances = self.service.list_instances().get_result()['instances']
        vsi_exists = False
        for instance in all_instances:
            if instance['name'] == self._generate_name('instance', job_key, call_id):
                logger.debug('{} Already exists. Need to find floating ip attached'.format(instance['name']))
                vsi_exists = True
                self.instance_id = instance['id']
        if (not vsi_exists):
            try:
                instance = self._create_instance(job_key, call_id)
                self.instance_id = instance['id']
                self.config['instance_id'] = self.instance_id
                logger.debug("VM {} created successfully ".format(instance['name']))
            except Exception as e:
                logger.error("There was an error trying to create the VM for {} {}".format(job_key, call_id))
                raise e

        try:
            floating_ip = self._create_and_attach_floating_ip(instance, job_key, call_id)
            logger.debug("VM {} updated successfully with floating IP {}".format(instance['name'], floating_ip))
            self.config['ip_address'] = floating_ip

            #what for we need this?
            #self._wait_instance_running(self.instance_id)
            return self.instance_id, floating_ip
        except Exception as e:
            logger.error("There was an error trying to to bind floating ip to vm {}".format(self.instance_id))
            self._delete_instance()
            raise e

    def stop(self):
        if self.config['delete_on_dismantle']:
            logger.info("Deleting VM instance")
            self._delete_instance()
            logger.debug("VM instance deleted successfully")
        else:
            logger.info("Stopping VM instance")
            self.create_instance_action('stop')
            logger.debug("VM instance stopped successfully")

    def get_runtime_key(self, runtime_name):
        runtime_key = os.path.join(self.name, self.ip_address,
                                   self.instance_id,
                                   runtime_name.strip("/"))

        return runtime_key
