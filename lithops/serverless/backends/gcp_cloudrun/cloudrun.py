#
# (C) Copyright Cloudlab URV 2020
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import logging
import httplib2
import os
import sys
import re
import time

from google.oauth2 import service_account
from google_auth_httplib2 import AuthorizedHttp
from google.auth.transport.requests import AuthorizedSession
from googleapiclient.discovery import build

from ..knative import config as kconfig
from . import config as cr_config
from ....utils import version_str, create_handler_zip
from ....version import __version__

logger = logging.getLogger(__name__)

CLOUDRUN_API_VERSION = 'v1'
SCOPES = ('https://www.googleapis.com/auth/cloud-platform',)


class GCPCloudRunBackend:
    def __init__(self, cloudrun_config, storage_config):
        self.credentials_path = cloudrun_config['credentials_path']
        self.service_account = cloudrun_config['service_account']
        self.project_name = cloudrun_config['project_name']
        self.region = cloudrun_config['region']

        self.runtime_cpus = cloudrun_config['runtime_cpus']
        self.container_runtime_concurrency = cloudrun_config['container_concurrency']
        self.workers = cloudrun_config['workers']

        self._invoker_sess = None
        self._invoker_sess_route = '/'
        self._service_url = None
        self._api_resource = None

    @staticmethod
    def _format_service_name(runtime_name, runtime_memory):
        return 'lithops--{}--{}--{}mb'.format(__version__.replace('.', '-'),
                                              runtime_name.replace('.', ''),
                                              runtime_memory)

    @staticmethod
    def _unformat_service_name(service_name):
        runtime_name, memory = service_name.rsplit('--', 1)
        image_name = runtime_name.replace('--', '/', 1)
        image_name = image_name.replace('--', ':', -1)
        return image_name, int(memory.replace('mb', ''))

    def _build_api_resource(self):
        if self._api_resource is None:
            logger.debug('Building admin API session')
            credentials = service_account.Credentials.from_service_account_file(self.credentials_path, scopes=SCOPES)
            http = AuthorizedHttp(credentials, http=httplib2.Http())
            self._api_resource = build('run', CLOUDRUN_API_VERSION,
                                       http=http,
                                       cache_discovery=False,
                                       client_options={
                                           'api_endpoint': 'https://{}-run.googleapis.com'.format(self.region)
                                       })
        return self._api_resource

    def _build_invoker_sess(self, runtime_name, memory, route):
        if self._invoker_sess is None or route != self._invoker_sess_route:
            logger.debug('Building invoker session')
            target = self._get_service_endpoint(runtime_name, memory) + route
            credentials = (service_account
                           .IDTokenCredentials
                           .from_service_account_file(self.credentials_path, target_audience=target))
            self._invoker_sess = AuthorizedSession(credentials)
            self._invoker_sess_route = route
        return self._invoker_sess

    def _get_service_endpoint(self, runtime_name, memory):
        if self._service_url is None:
            logger.debug('Getting service endpoint')
            res = self._build_api_resource().namespaces().services().get(
                name='namespaces/{}/services/{}'.format(self.project_name,
                                                        self._format_service_name(runtime_name, memory))
            ).execute()
            self._service_url = res['status']['url']
        return self._service_url

    def _format_image_name(self, runtime_name):
        runtime_name = runtime_name.replace('.', '').replace('_', '-')
        revision = 'latest' if 'dev' in __version__ else __version__.replace('.', '')
        return 'gcr.io/{}/lithops-{}:{}'.format(self.project_name, runtime_name, revision)

    def _build_default_runtime(self):
        """
        Builds the default runtime
        """
        logger.debug('Building default {} runtime'.format(cr_config.DEFAULT_RUNTIME_NAME))
        if os.system('{} --version >{} 2>&1'.format(kconfig.DOCKER_PATH, os.devnull)) == 0:
            # Build default runtime using local dokcer
            python_version = version_str(sys.version_info)
            dockerfile = "Dockefile.default-knative-runtime"
            with open(dockerfile, 'w') as f:
                f.write("FROM python:{}-slim-buster\n".format(python_version))
                f.write(cr_config.DEFAULT_DOCKERFILE)
            self.build_runtime(self._format_image_name(cr_config.DEFAULT_RUNTIME_NAME), dockerfile)
            os.remove(dockerfile)
        else:
            raise Exception('Docker CLI not found')

    def _generate_runtime_meta(self, runtime_name, memory):
        """
        Extract installed Python modules from docker image
        """
        logger.info("Extracting Python modules from: {}".format(runtime_name))

        try:
            runtime_meta = self.invoke(runtime_name, memory,
                                       {'service_route': '/preinstalls'}, return_result=True)
        except Exception as e:
            raise Exception("Unable to extract the preinstalled modules from the runtime: {}".format(e))

        if not runtime_meta or 'preinstalls' not in runtime_meta:
            raise Exception('Failed getting runtime metadata: {}'.format(runtime_meta))

        return runtime_meta

    def invoke(self, runtime_name, memory, payload, return_result=False):
        exec_id = payload.get('executor_id')
        call_id = payload.get('call_id')
        job_id = payload.get('job_id')
        route = payload.get("service_route", '/')

        sess = self._build_invoker_sess(runtime_name, memory, route)

        if exec_id and job_id and call_id:
            logger.debug('ExecutorID {} | JobID {} - Invoking function call {}'
                         .format(exec_id, job_id, call_id))
        elif exec_id and job_id:
            logger.debug('ExecutorID {} | JobID {} - Invoking function'
                         .format(exec_id, job_id))
        else:
            logger.debug('Invoking function')

        res = sess.post(url=self._get_service_endpoint(runtime_name, memory) + route, json=payload)

        if res.status_code in (200, 202):
            data = res.json()
            if return_result:
                return data
            return data["activationId"]

    def build_runtime(self, docker_image_name, dockerfile):
        logger.debug('Building a new docker image from Dockerfile')
        logger.debug('Docker image name: {}'.format(docker_image_name))

        expression = '^gcr.io/([a-z0-9]+)/([-a-z0-9]+)(:[a-z0-9]+)?'
        result = re.match(expression, docker_image_name)

        if not result or result.group() != docker_image_name:
            raise Exception("Invalid docker image name: All letters must be "
                            "lowercase and '.' or '_' characters are not allowed")

        entry_point = os.path.join(os.path.dirname(__file__), 'entry_point.py')
        create_handler_zip(kconfig.FH_ZIP_LOCATION, entry_point, 'lithopsproxy.py')

        if dockerfile:
            cmd = '{} build -t {} -f {} .'.format(kconfig.DOCKER_PATH,
                                                  docker_image_name,
                                                  dockerfile)
        else:
            cmd = '{} build -t {} .'.format(kconfig.DOCKER_PATH, docker_image_name)

        logger.info('Building default runtime')
        if logger.getEffectiveLevel() != logging.DEBUG:
            cmd = cmd + " >{} 2>&1".format(os.devnull)

        res = os.system(cmd)
        if res != 0:
            raise Exception('There was an error building the runtime')

        os.remove(kconfig.FH_ZIP_LOCATION)

        cmd = '{} push {}'.format(kconfig.DOCKER_PATH, docker_image_name)
        if logger.getEffectiveLevel() != logging.DEBUG:
            cmd = cmd + " >{} 2>&1".format(os.devnull)
        res = os.system(cmd)
        if res != 0:
            raise Exception('There was an error pushing the runtime to the container registry')

    def create_runtime(self, runtime_name, memory, timeout):
        if runtime_name == cr_config.DEFAULT_RUNTIME_NAME:
            self._build_default_runtime()

        img_name = self._format_image_name(runtime_name)

        service_name = self._format_service_name(runtime_name, memory)

        body = {
            "apiVersion": 'serving.knative.dev/v1',
            "kind": 'Service',
            "metadata": {
                "name": service_name,
                "namespace": self.project_name,
                "annotations": {
                    "autoscaling.knative.dev/maxScale": str(self.workers)
                }
            },
            "spec": {
                "template": {
                    "metadata": {
                        "name": '{}-rev'.format(service_name),
                        "namespace": self.project_name,
                    },
                    "spec": {
                        "containerConcurrency": self.container_runtime_concurrency,
                        "timeoutSeconds": timeout,
                        "serviceAccountName": self.service_account,
                        "containers": [
                            {
                                "image": img_name,
                                "resources": {
                                    "limits": {
                                        "memory": "{}Mi".format(memory),
                                        "cpu": str(self.runtime_cpus)
                                    },
                                },
                            }
                        ],
                    }
                },
                "traffic": [
                    {
                        "percent": 100,
                        "latestRevision": True
                    }
                ]
            }
        }

        res = self._build_api_resource().namespaces().services().create(
            parent='namespaces/{}'.format(self.project_name),
            body=body
        ).execute()

        # Wait until service is up
        ready = False
        retry = 15
        while not ready:
            res = self._build_api_resource().namespaces().services().get(
                name='namespaces/{}/services/{}'.format(self.project_name,
                                                        self._format_service_name(runtime_name, memory))
            ).execute()

            ready = all(cond['status'] == 'True' for cond in res['status']['conditions'])

            if not ready:
                logger.debug('Waiting until service is up...')
                time.sleep((1 / retry) * 25)
                retry -= 1
                if retry == 0:
                    raise Exception('Maximum retries reached: {}'.format(res))
            else:
                self._service_url = res['status']['url']

        runtime_meta = self._generate_runtime_meta(runtime_name, memory)
        return runtime_meta

    def delete_runtime(self, runtime_name, memory):
        pass

    def clean(self):
        pass

    def clear(self):
        pass

    def list_runtimes(self, runtime_name='all'):
        pass

    def get_runtime_key(self, runtime_name, memory):
        service_name = self._format_service_name(runtime_name, memory)
        runtime_key = os.path.join(self.project_name, service_name)

        return runtime_key
