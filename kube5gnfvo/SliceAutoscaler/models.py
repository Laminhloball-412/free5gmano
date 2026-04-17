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

import uuid
from django.db import models


class SlicePolicy(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    nsInstanceId = models.UUIDField(unique=True)
    enabled = models.BooleanField(default=True)

    # CPU thresholds (percentage 0-100)
    cpu_scale_out_threshold = models.IntegerField(default=80)
    cpu_scale_in_threshold = models.IntegerField(default=20)

    # Memory thresholds (percentage 0-100)
    memory_scale_out_threshold = models.IntegerField(default=80)
    memory_scale_in_threshold = models.IntegerField(default=20)

    # Scaling limits
    max_replicas = models.IntegerField(default=10)
    min_replicas = models.IntegerField(default=1)

    # Cooldown: seconds to wait between consecutive scaling actions
    cooldown_period = models.IntegerField(default=120)

    # Auto-heal: automatically restart crashed VNFs
    auto_heal = models.BooleanField(default=True)

    # How often the controller checks this slice (seconds)
    check_interval = models.IntegerField(default=30)

    last_action_time = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return 'SlicePolicy(ns={}, enabled={})'.format(self.nsInstanceId, self.enabled)


class SliceActionLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    policy = models.ForeignKey(SlicePolicy, on_delete=models.CASCADE,
                               related_name='action_logs')
    nsInstanceId = models.UUIDField()
    action_type = models.TextField()  # SCALE_OUT, SCALE_IN, HEAL
    vnf_instance_id = models.TextField(null=True, blank=True)
    reason = models.TextField()
    metrics_snapshot = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return 'SliceAction({}, {})'.format(self.action_type, self.nsInstanceId)
