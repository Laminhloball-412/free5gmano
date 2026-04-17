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

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.exceptions import APIException
from rest_framework.response import Response

from NSLifecycleManagement.models import NsInstance
from SliceAutoscaler.models import SlicePolicy, SliceActionLog
from SliceAutoscaler.serializers import SlicePolicySerializer, SliceActionLogSerializer
from VIMManagement.utils.metrics_collector import MetricsCollector


class SlicePolicyViewSet(viewsets.ModelViewSet):
    queryset = SlicePolicy.objects.all()
    serializer_class = SlicePolicySerializer

    def create(self, request, **kwargs):
        """Create a dynamic slice policy for an NS instance."""
        ns_id = request.data.get('nsInstanceId')
        if not ns_id:
            raise APIException(detail='nsInstanceId is required',
                               code=status.HTTP_400_BAD_REQUEST)

        ns_instance = NsInstance.objects.filter(id=ns_id).last()
        if not ns_instance:
            raise APIException(detail='NS instance not found',
                               code=status.HTTP_404_NOT_FOUND)

        if SlicePolicy.objects.filter(nsInstanceId=ns_id).exists():
            raise APIException(detail='Policy already exists for this NS instance',
                               code=status.HTTP_409_CONFLICT)

        return super().create(request)

    @action(detail=True, methods=['GET'], url_path='metrics')
    def get_metrics(self, request, **kwargs):
        """Get real-time metrics for VNFs in the NS instance bound to this policy."""
        policy = self.get_object()
        ns_instance = NsInstance.objects.filter(id=policy.nsInstanceId).last()
        if not ns_instance or ns_instance.nsState != 'INSTANTIATED':
            raise APIException(detail='NS instance is not instantiated')

        collector = MetricsCollector()
        vnf_metrics = {}

        for vnf in ns_instance.NsInstance_VnfInstance.all():
            vnf_name = vnf.vnfInstanceName.lower()
            # Try common namespaces
            usage = collector.get_deployment_resource_usage(vnf_name, 'default')
            if usage:
                vnf_metrics[str(vnf.id)] = {
                    'vnfInstanceName': vnf.vnfInstanceName,
                    'cpu_percent': usage['cpu_percent'],
                    'memory_percent': usage['memory_percent'],
                    'current_replicas': usage['current_replicas'],
                    'desired_replicas': usage['desired_replicas']
                }

        return Response({
            'nsInstanceId': str(policy.nsInstanceId),
            'vnfMetrics': vnf_metrics
        })

    @action(detail=True, methods=['GET'], url_path='logs')
    def get_action_logs(self, request, **kwargs):
        """Get the action log for this policy (auto-scale/heal decisions)."""
        policy = self.get_object()
        logs = SliceActionLog.objects.filter(policy=policy).order_by('-created_at')[:50]
        serializer = SliceActionLogSerializer(logs, many=True)
        return Response(serializer.data)
