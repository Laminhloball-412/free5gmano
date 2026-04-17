# All Rights Reserved.
#
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

import logging
from VIMManagement.utils.base_kubernetes import BaseKubernetes

logger = logging.getLogger(__name__)


class MetricsCollector(BaseKubernetes):
    """Collects real-time CPU and memory metrics from the Kubernetes Metrics API.

    Uses the metrics.k8s.io API (provided by metrics-server) to fetch pod-level
    resource usage for VNF deployments.
    """

    def __init__(self):
        super().__init__()

    def get_pod_metrics(self, namespace='default'):
        """Fetch pod metrics from the K8s Metrics API (metrics.k8s.io/v1beta1)."""
        try:
            metrics = self.api_crd.list_namespaced_custom_object(
                group='metrics.k8s.io',
                version='v1beta1',
                namespace=namespace,
                plural='pods'
            )
            return metrics.get('items', [])
        except self.ApiException as e:
            logger.error('Failed to fetch pod metrics from namespace %s: %s', namespace, e)
            return []

    def get_node_metrics(self):
        """Fetch node-level metrics."""
        try:
            metrics = self.api_crd.list_cluster_custom_object(
                group='metrics.k8s.io',
                version='v1beta1',
                plural='nodes'
            )
            return metrics.get('items', [])
        except self.ApiException as e:
            logger.error('Failed to fetch node metrics: %s', e)
            return []

    def get_deployment_metrics(self, deployment_name, namespace='default'):
        """Get aggregated CPU and memory usage for all pods in a deployment.

        Returns dict: {
            'cpu_usage_millicores': int,
            'memory_usage_bytes': int,
            'pod_count': int,
            'pods': [{'name': str, 'cpu': int, 'memory': int}, ...]
        }
        """
        pod_metrics = self.get_pod_metrics(namespace)
        result = {
            'cpu_usage_millicores': 0,
            'memory_usage_bytes': 0,
            'pod_count': 0,
            'pods': []
        }

        for pod in pod_metrics:
            pod_name = pod['metadata']['name']
            if deployment_name not in pod_name:
                continue

            pod_cpu = 0
            pod_memory = 0
            for container in pod.get('containers', []):
                cpu_str = container['usage'].get('cpu', '0')
                mem_str = container['usage'].get('memory', '0')
                pod_cpu += self._parse_cpu(cpu_str)
                pod_memory += self._parse_memory(mem_str)

            result['cpu_usage_millicores'] += pod_cpu
            result['memory_usage_bytes'] += pod_memory
            result['pod_count'] += 1
            result['pods'].append({
                'name': pod_name,
                'cpu_millicores': pod_cpu,
                'memory_bytes': pod_memory
            })

        return result

    def get_deployment_resource_usage(self, deployment_name, namespace='default'):
        """Get CPU and memory usage as a percentage of requested resources.

        Returns dict: {
            'cpu_percent': float,
            'memory_percent': float,
            'current_replicas': int,
            'desired_replicas': int,
            'raw': <raw metrics dict>
        }
        """
        metrics = self.get_deployment_metrics(deployment_name, namespace)

        try:
            deployment = self.app_v1.read_namespaced_deployment(
                name=deployment_name, namespace=namespace)
        except self.ApiException:
            logger.warning('Deployment %s not found in namespace %s', deployment_name, namespace)
            return None

        spec = deployment.spec
        desired_replicas = spec.replicas or 1
        current_replicas = metrics['pod_count']

        containers = spec.template.spec.containers
        total_cpu_request = 0
        total_memory_request = 0
        for container in containers:
            if container.resources and container.resources.requests:
                cpu_req = container.resources.requests.get('cpu', '0')
                mem_req = container.resources.requests.get('memory', '0')
                total_cpu_request += self._parse_cpu(cpu_req)
                total_memory_request += self._parse_memory(mem_req)

        # Scale requests by replica count
        total_cpu_request *= desired_replicas
        total_memory_request *= desired_replicas

        cpu_percent = 0.0
        memory_percent = 0.0
        if total_cpu_request > 0:
            cpu_percent = (metrics['cpu_usage_millicores'] / total_cpu_request) * 100
        if total_memory_request > 0:
            memory_percent = (metrics['memory_usage_bytes'] / total_memory_request) * 100

        return {
            'cpu_percent': round(cpu_percent, 2),
            'memory_percent': round(memory_percent, 2),
            'current_replicas': current_replicas,
            'desired_replicas': desired_replicas,
            'raw': metrics
        }

    @staticmethod
    def _parse_cpu(cpu_str):
        """Parse CPU string to millicores.
        Examples: '100m' -> 100, '1' -> 1000, '250m' -> 250, '0' -> 0
        """
        cpu_str = str(cpu_str)
        if cpu_str.endswith('n'):
            return int(cpu_str[:-1]) // 1000000
        elif cpu_str.endswith('u'):
            return int(cpu_str[:-1]) // 1000
        elif cpu_str.endswith('m'):
            return int(cpu_str[:-1])
        else:
            return int(float(cpu_str) * 1000)

    @staticmethod
    def _parse_memory(mem_str):
        """Parse memory string to bytes.
        Examples: '128Mi' -> 134217728, '1Gi' -> 1073741824, '512Ki' -> 524288
        """
        mem_str = str(mem_str)
        units = {
            'Ki': 1024,
            'Mi': 1024 ** 2,
            'Gi': 1024 ** 3,
            'Ti': 1024 ** 4,
            'K': 1000,
            'M': 1000 ** 2,
            'G': 1000 ** 3,
            'T': 1000 ** 4,
        }
        for suffix, multiplier in units.items():
            if mem_str.endswith(suffix):
                return int(mem_str[:-len(suffix)]) * multiplier
        return int(mem_str)
