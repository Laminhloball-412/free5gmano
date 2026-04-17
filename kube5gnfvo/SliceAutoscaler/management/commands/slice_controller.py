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
Django management command to run the dynamic slice controller.

Usage:
    python manage.py slice_controller
    python manage.py slice_controller --interval 15

This starts a long-running control loop that:
1. Fetches all enabled SlicePolicies
2. For each policy, collects K8s metrics for the NS instance's VNFs
3. Evaluates metrics against policy thresholds
4. Automatically executes scale-out, scale-in, or heal actions
5. Logs all decisions to SliceActionLog and NsLcmOpOcc
"""

import logging
import time

from django.core.management.base import BaseCommand

from SliceAutoscaler.engine import SliceController
from SliceAutoscaler.models import SlicePolicy

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Run the dynamic slice controller loop'

    def add_arguments(self, parser):
        parser.add_argument(
            '--interval',
            type=int,
            default=30,
            help='Default polling interval in seconds (overridden per-policy by check_interval)'
        )

    def handle(self, *args, **options):
        base_interval = options['interval']
        controller = SliceController()

        self.stdout.write(self.style.SUCCESS(
            'Dynamic Slice Controller started (base interval={}s)'.format(base_interval)))

        while True:
            policies = SlicePolicy.objects.filter(enabled=True)

            if not policies.exists():
                logger.debug('No enabled slice policies, sleeping...')
                time.sleep(base_interval)
                continue

            for policy in policies:
                try:
                    actions = controller.evaluate(policy)
                    for act in actions:
                        self.stdout.write(self.style.WARNING(
                            '[AUTO] {} vnf={} reason={}'.format(
                                act['action'], act['vnf'], act['reason'])))
                except Exception as e:
                    logger.error('Error evaluating policy %s: %s', policy.id, e)

            time.sleep(base_interval)
