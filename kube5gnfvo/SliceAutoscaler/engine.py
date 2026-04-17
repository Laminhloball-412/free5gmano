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

"""
Closed-loop slice controller engine.

Continuously monitors instantiated Network Slices and automatically adjusts
their state (scale-out, scale-in, heal) based on real-time K8s metrics and
the policies defined in SlicePolicy.

Control loop:  Collect Metrics → Evaluate Policies → Execute Actions → Log
"""

import json
import logging
import threading
from functools import partial

from django.utils import timezone

from NSLifecycleManagement.models import NsInstance, VnfInstance
from NSLCMOperationOccurrences.models import NsLcmOpOcc, ResourceChanges
from SliceAutoscaler.models import SlicePolicy, SliceActionLog
from VIMManagement.utils.metrics_collector import MetricsCollector
from VnfPackageManagement.models import VnfPkgInfo
from utils.process_package.create_vnf import CreateService
from utils.process_package.delete_vnf import DeleteService

logger = logging.getLogger(__name__)


class SliceController(object):
    """Evaluates a single NS instance against its policy and takes action."""

    def __init__(self):
        self.metrics_collector = MetricsCollector()

    def evaluate(self, policy):
        """Run one evaluation cycle for a policy.

        Returns list of actions taken: [{'action': str, 'vnf': str, 'reason': str}, ...]
        """
        ns_instance = NsInstance.objects.filter(id=policy.nsInstanceId).last()
        if not ns_instance or ns_instance.nsState != 'INSTANTIATED':
            return []

        if not self._cooldown_ok(policy):
            return []

        actions = []

        for vnf_instance in ns_instance.NsInstance_VnfInstance.all():
            vnf_name = vnf_instance.vnfInstanceName.lower()

            # Determine namespace from TOSCA (default if unknown)
            namespace = self._get_vnf_namespace(vnf_instance)

            usage = self.metrics_collector.get_deployment_resource_usage(vnf_name, namespace)
            if usage is None:
                continue

            metrics_snap = json.dumps({
                'cpu_percent': usage['cpu_percent'],
                'memory_percent': usage['memory_percent'],
                'current_replicas': usage['current_replicas'],
                'desired_replicas': usage['desired_replicas']
            })

            # --- Heal: pod count < desired replicas indicates crash ---
            if (policy.auto_heal and
                    usage['current_replicas'] < usage['desired_replicas']):
                action = self._do_heal(policy, ns_instance, vnf_instance, metrics_snap)
                if action:
                    actions.append(action)
                continue

            # --- Scale-out ---
            if (usage['cpu_percent'] > policy.cpu_scale_out_threshold or
                    usage['memory_percent'] > policy.memory_scale_out_threshold):
                if usage['desired_replicas'] < policy.max_replicas:
                    action = self._do_scale_out(
                        policy, ns_instance, vnf_instance, usage, metrics_snap)
                    if action:
                        actions.append(action)
                continue

            # --- Scale-in ---
            if (usage['cpu_percent'] < policy.cpu_scale_in_threshold and
                    usage['memory_percent'] < policy.memory_scale_in_threshold):
                if usage['desired_replicas'] > policy.min_replicas:
                    action = self._do_scale_in(
                        policy, ns_instance, vnf_instance, usage, metrics_snap)
                    if action:
                        actions.append(action)

        return actions

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _do_scale_out(self, policy, ns_instance, vnf_instance, usage, metrics_snap):
        new_replicas = min(usage['desired_replicas'] + 1, policy.max_replicas)
        reason = 'CPU={:.1f}% (>{}) or MEM={:.1f}% (>{})'.format(
            usage['cpu_percent'], policy.cpu_scale_out_threshold,
            usage['memory_percent'], policy.memory_scale_out_threshold)

        logger.info('SCALE_OUT ns=%s vnf=%s replicas=%d->%d reason=%s',
                     ns_instance.id, vnf_instance.vnfInstanceName,
                     usage['desired_replicas'], new_replicas, reason)

        try:
            create_svc = CreateService(vnf_instance.vnfPkgId, vnf_instance.vnfInstanceName)
            threading.Thread(
                target=partial(create_svc.process_instance, replicas=new_replicas),
                daemon=True
            ).start()

            self._record(policy, ns_instance, vnf_instance, 'SCALE_OUT', reason, metrics_snap)
            return {'action': 'SCALE_OUT', 'vnf': vnf_instance.vnfInstanceName, 'reason': reason}
        except Exception as e:
            logger.error('SCALE_OUT failed: %s', e)
            return None

    def _do_scale_in(self, policy, ns_instance, vnf_instance, usage, metrics_snap):
        new_replicas = max(usage['desired_replicas'] - 1, policy.min_replicas)
        reason = 'CPU={:.1f}% (<{}) and MEM={:.1f}% (<{})'.format(
            usage['cpu_percent'], policy.cpu_scale_in_threshold,
            usage['memory_percent'], policy.memory_scale_in_threshold)

        logger.info('SCALE_IN ns=%s vnf=%s replicas=%d->%d reason=%s',
                     ns_instance.id, vnf_instance.vnfInstanceName,
                     usage['desired_replicas'], new_replicas, reason)

        try:
            create_svc = CreateService(vnf_instance.vnfPkgId, vnf_instance.vnfInstanceName)
            threading.Thread(
                target=partial(create_svc.process_instance, replicas=new_replicas),
                daemon=True
            ).start()

            self._record(policy, ns_instance, vnf_instance, 'SCALE_IN', reason, metrics_snap)
            return {'action': 'SCALE_IN', 'vnf': vnf_instance.vnfInstanceName, 'reason': reason}
        except Exception as e:
            logger.error('SCALE_IN failed: %s', e)
            return None

    def _do_heal(self, policy, ns_instance, vnf_instance, metrics_snap):
        reason = 'Pod count below desired replicas (possible crash)'

        logger.info('HEAL ns=%s vnf=%s reason=%s',
                     ns_instance.id, vnf_instance.vnfInstanceName, reason)

        try:
            # Delete then re-create the VNF deployment to heal it
            delete_svc = DeleteService(vnf_instance.vnfPkgId, vnf_instance.vnfInstanceName)
            create_svc = CreateService(vnf_instance.vnfPkgId, vnf_instance.vnfInstanceName)

            def _heal_sequence():
                delete_svc.process_instance()
                create_svc.process_instance()

            threading.Thread(target=_heal_sequence, daemon=True).start()

            self._record_lcm_op_occ(ns_instance, vnf_instance, 'Heal')
            self._record(policy, ns_instance, vnf_instance, 'HEAL', reason, metrics_snap)
            return {'action': 'HEAL', 'vnf': vnf_instance.vnfInstanceName, 'reason': reason}
        except Exception as e:
            logger.error('HEAL failed: %s', e)
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _cooldown_ok(self, policy):
        """Return True if enough time has passed since the last action."""
        if policy.last_action_time is None:
            return True
        elapsed = (timezone.now() - policy.last_action_time).total_seconds()
        return elapsed >= policy.cooldown_period

    def _get_vnf_namespace(self, vnf_instance):
        """Attempt to determine the K8s namespace for a VNF.
        Falls back to 'default' if not determinable.
        """
        try:
            vnf_pkg = VnfPkgInfo.objects.filter(id=vnf_instance.vnfPkgId).last()
            if vnf_pkg:
                from utils.process_package.process_vnf_instance import ProcessVNFInstance
                proc = ProcessVNFInstance(str(vnf_pkg.id), vnf_instance.vnfInstanceName)
                for vdu in proc.tosca.topology_template.vdu_template:
                    if 'namespace' in vdu.attributes:
                        return vdu.attributes['namespace']
        except Exception:
            pass
        return 'default'

    def _record(self, policy, ns_instance, vnf_instance, action_type, reason, metrics_snap):
        """Write action to SliceActionLog and update policy cooldown."""
        SliceActionLog.objects.create(
            policy=policy,
            nsInstanceId=ns_instance.id,
            action_type=action_type,
            vnf_instance_id=str(vnf_instance.id),
            reason=reason,
            metrics_snapshot=metrics_snap
        )
        policy.last_action_time = timezone.now()
        policy.save(update_fields=['last_action_time'])

    def _record_lcm_op_occ(self, ns_instance, vnf_instance, operation_type):
        """Record an automatic LCM operation occurrence."""
        occ = NsLcmOpOcc.objects.create(
            nsInstanceId=str(ns_instance.id),
            statusEnteredTime=timezone.now(),
            lcmOperationType=operation_type,
            isAutomaticInvocation=True,
            isCancelPending=False,
            operationParams=json.dumps({
                'vnfInstanceId': str(vnf_instance.id),
                'trigger': 'SliceAutoscaler'
            })
        )
        resource_changes = ResourceChanges.objects.create(resourceChanges=occ)
        resource_changes.affectedVnfs.create(
            vnfInstanceId=str(vnf_instance.id),
            vnfdId=vnf_instance.vnfdId,
            vnfProfileId=vnf_instance.vnfPkgId,
            vnfName=vnf_instance.vnfInstanceName,
            changeType=operation_type.upper(),
            changeResult='COMPLETED'
        )
