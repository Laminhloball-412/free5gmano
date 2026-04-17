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
import json
import logging
import threading

from NSLifecycleManagement.models import VnfInstance, NsInstance
from NSFaultManagement.models import Alarm, AlarmLinks, FaultyComponentInfo, FaultyResourceInfo
from datetime import datetime

from utils.notification_management.kafka_notification import KafkaNotification

logger = logging.getLogger(__name__)


class AlarmEvent(object):
    error_record = dict()
    kafka_notification = KafkaNotification('fault_alarm')

    def managed_object(self, vnf_instance):
        ns_instance_set = NsInstance.objects.filter(
            NsInstance_VnfInstance__vnfInstanceName=vnf_instance.vnfInstanceName)
        ns_instance_id = list()
        ns_instance_link = list()
        for ns_instance in ns_instance_set:
            ns_instance_id.append(str(ns_instance.id))
            ns_instance_link.append(str(ns_instance.NsInstance_links.link_self))

        return json.dumps(ns_instance_id), json.dumps(ns_instance_link)

    def create_alarm(self, name: str, reason: str, message: str, is_container: bool):
        if is_container:
            pod_name_list = name.split('-')
            [pod_name_list.pop(-1) for _ in range(0, 2)]
            vnf_name = '-'.join(pod_name_list)
        else:
            vnf_name = name[:-5]

        vnf_instance = VnfInstance.objects.filter(vnfInstanceName=vnf_name).last()
        if vnf_instance:
            ns_instance_id, ns_instance_link = self.managed_object(vnf_instance)
            check = self._time_check(ns_instance_id, str(vnf_instance.id))
            if check:
                self.kafka_notification.notify(ns_instance_id, 'NS Instance({}) crashed'.format(ns_instance_id))
                alarm = Alarm.objects.create(
                    **{'managedObjectId': ns_instance_id,
                       'probableCause': reason,
                       'faultDetails': message})

                AlarmLinks.objects.create(
                    _links=alarm,
                    **{'link_self': 'nsfm/v1/alarms/{}'.format(alarm.id),
                       'objectInstance': ns_instance_link})

                FaultyComponentInfo.objects.create(
                    rootCauseFaultyComponent=alarm,
                    **{'faultyVnfInstanceId': str(vnf_instance.id)}
                )

                FaultyResourceInfo.objects.create(rootCauseFaultyResource=alarm)

                # Trigger auto-heal if a SlicePolicy with auto_heal=True exists
                self._trigger_auto_heal(vnf_instance)

    def _trigger_auto_heal(self, vnf_instance):
        """Automatically heal a crashed VNF if its NS has an auto-heal policy."""
        try:
            from SliceAutoscaler.models import SlicePolicy, SliceActionLog
            from utils.process_package.create_vnf import CreateService
            from utils.process_package.delete_vnf import DeleteService

            ns_instances = NsInstance.objects.filter(
                NsInstance_VnfInstance__vnfInstanceName=vnf_instance.vnfInstanceName,
                nsState='INSTANTIATED')

            for ns_instance in ns_instances:
                policy = SlicePolicy.objects.filter(
                    nsInstanceId=ns_instance.id, enabled=True, auto_heal=True).first()
                if not policy:
                    continue

                logger.info('Auto-healing VNF %s in NS %s (triggered by alarm)',
                            vnf_instance.vnfInstanceName, ns_instance.id)

                delete_svc = DeleteService(vnf_instance.vnfPkgId, vnf_instance.vnfInstanceName)
                create_svc = CreateService(vnf_instance.vnfPkgId, vnf_instance.vnfInstanceName)

                def _heal_sequence():
                    delete_svc.process_instance()
                    create_svc.process_instance()

                threading.Thread(target=_heal_sequence, daemon=True).start()

                SliceActionLog.objects.create(
                    policy=policy,
                    nsInstanceId=ns_instance.id,
                    action_type='HEAL',
                    vnf_instance_id=str(vnf_instance.id),
                    reason='Auto-heal triggered by CrashLoopBackOff alarm'
                )
        except Exception as e:
            logger.error('Auto-heal trigger failed: %s', e)

    def _time_check(self, ns_id, vnf_id):
        if ns_id + vnf_id in list(self.error_record):
            time = str(datetime.now() - self.error_record[ns_id + vnf_id]).split('.')[0].split(':')
            if int(time[0]) >= 1 or int(time[1]) >= 1 or int(time[2]) >= 10:
                self.error_record[ns_id + vnf_id] = datetime.now()
                return True
            return False
        else:
            self.error_record[ns_id + vnf_id] = datetime.now()
            return True
