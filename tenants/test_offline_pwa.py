from django.test import RequestFactory, SimpleTestCase
from django.template.loader import get_template

from tenants.views import service_worker


class OfflineServiceWorkerTest(SimpleTestCase):
    def setUp(self):
        self.response = service_worker(RequestFactory().get("/service-worker.js"))
        self.script = self.response.content.decode()

    def test_worker_controls_the_entire_tenant_origin_without_being_http_cached(self):
        self.assertEqual(self.response["Service-Worker-Allowed"], "/")
        self.assertEqual(self.response["Cache-Control"], "no-cache")

    def test_worker_precaches_connectivity_and_offline_console_assets(self):
        self.assertIn("/static/js/connectivity.js", self.script)
        self.assertIn("/static/js/offline-reception.js", self.script)
        self.assertIn("caches.match('/offline/')", self.script)

    def test_worker_has_a_last_resort_navigation_response(self):
        self.assertIn("No internet connection", self.script)
        self.assertIn("location.reload()", self.script)

    def test_authenticated_shell_warms_offline_reception_cache(self):
        template_source = get_template("base.html").template.source
        self.assertIn("fetch('/offline/?warm=1'", template_source)
        self.assertIn("X-Circle-Core-Offline-Warmup", template_source)
