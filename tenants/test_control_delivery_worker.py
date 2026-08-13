from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from tenants.models import ControlDeliveryWorkerHeartbeat
from tenants.product_control_backend import GuestHouseProductControlBackend


class ControlDeliveryWorkerTests(TestCase):
    @patch("tenants.management.commands.run_control_delivery_worker.call_command")
    def test_once_dispatches_product_owned_delivery_and_nonce_cleanup(self, delegated):
        call_command("run_control_delivery_worker", once=True, verbosity=0)
        self.assertEqual(
            [call.args[0] for call in delegated.call_args_list],
            [
                "dispatch_control_activations",
                "dispatch_control_operation_notifications",
                "purge_control_api_nonces",
            ],
        )
        heartbeat = ControlDeliveryWorkerHeartbeat.objects.get(name='control-delivery')
        self.assertIsNotNone(heartbeat.last_success_at)
        self.assertFalse(heartbeat.last_error_code)
        self.assertEqual(GuestHouseProductControlBackend().health(None)['status'], 'healthy')

    def test_health_degrades_when_delivery_worker_has_no_heartbeat(self):
        self.assertEqual(GuestHouseProductControlBackend().health(None)['status'], 'degraded')
