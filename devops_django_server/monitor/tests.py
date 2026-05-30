from django.test import SimpleTestCase

from monitor.models import MonitorDomainDiagnosis
from monitor.dns_diagnosis import QTYPE_A, QTYPE_NS, classify_dns_evidence, normalize_hostname
from monitor.management.commands.worker import _format_alert_message, _format_dns_misconfig_alert_message


class DomainDiagnosisTests(SimpleTestCase):
    def test_normalize_hostname_accepts_url_or_plain_domain(self):
        self.assertEqual(normalize_hostname("https://www.example.com/path"), "www.example.com")
        self.assertEqual(normalize_hostname("example.com/path"), "example.com")

    def test_registrar_hold_wins_when_no_address(self):
        result = classify_dns_evidence(
            hostname="example.com",
            address_results=[
                {"resolver": "1.1.1.1", "qtype": QTYPE_A, "rcode": "NXDOMAIN", "answers": []},
            ],
            ns_results=[],
            rdap={"ok": True, "statuses": ["client hold"]},
            registrar_ns_patterns=[],
        )
        self.assertEqual(result.diagnosis_type, "registrar_hold")
        self.assertGreaterEqual(result.confidence, 0.9)

    def test_registrar_dns_suspended_requires_matching_ns_and_dns_failures(self):
        result = classify_dns_evidence(
            hostname="example.com",
            address_results=[
                {"resolver": "223.5.5.5", "qtype": QTYPE_A, "rcode": "SERVFAIL", "answers": []},
                {"resolver": "119.29.29.29", "qtype": QTYPE_A, "rcode": "REFUSED", "answers": []},
            ],
            ns_results=[
                {
                    "resolver": "223.5.5.5",
                    "qtype": QTYPE_NS,
                    "rcode": "NOERROR",
                    "answers": [{"type": QTYPE_NS, "value": "ns1.juming.com"}],
                }
            ],
            rdap=None,
            registrar_ns_patterns=["juming"],
        )
        self.assertEqual(result.diagnosis_type, "registrar_dns_suspended")
        self.assertGreaterEqual(result.confidence, 0.8)

    def test_address_answer_downgrades_to_http_only_failure(self):
        result = classify_dns_evidence(
            hostname="example.com",
            address_results=[
                {"resolver": "1.1.1.1", "qtype": QTYPE_A, "rcode": "NOERROR", "answers": [{"type": QTYPE_A, "value": "93.184.216.34"}]},
                {"resolver": "8.8.8.8", "qtype": QTYPE_A, "rcode": "TIMEOUT", "answers": []},
            ],
            ns_results=[],
            rdap=None,
            registrar_ns_patterns=["juming"],
        )
        self.assertEqual(result.diagnosis_type, "http_only_failure")

    def test_dns_misconfig_alert_message_uses_chinese_diagnosis(self):
        diagnosis = MonitorDomainDiagnosis(
            domain="https://example.com",
            diagnosis_type=MonitorDomainDiagnosis.DiagnosisType.DNS_MISCONFIG,
            confidence=0.65,
            evidence={
                "address_failures": ["SERVFAIL", "REFUSED"],
                "address_results": [
                    {"resolver": "1.1.1.1", "rcode": "SERVFAIL"},
                    {"resolver": "8.8.8.8", "rcode": "REFUSED"},
                ],
                "ns_names": ["ns1.example-dns.com"],
            },
        )

        msg = _format_dns_misconfig_alert_message("https://example.com", diagnosis)

        self.assertIn("DNS 配置异常，解析服务不可用", msg)
        self.assertIn("DNS诊断=DNS 配置异常", msg)
        self.assertIn("SERVFAIL", msg)
        self.assertIn("1.1.1.1", msg)

    def test_fail_rate_alert_message_includes_dns_summary_when_present(self):
        diagnosis = MonitorDomainDiagnosis(
            domain="https://example.com/not-exist",
            diagnosis_type=MonitorDomainDiagnosis.DiagnosisType.HTTP_ONLY_FAILURE,
            confidence=0.75,
            evidence={},
        )

        msg = _format_alert_message(
            "https://example.com/not-exist",
            threshold=0.3,
            total=36,
            rate1=1.0,
            rate2=1.0,
            primary_platform="chinaz",
            retest_platform="itdog",
            diagnosis=diagnosis,
        )

        self.assertIn("DNS诊断: HTTP 访问异常", msg)
        self.assertIn("DNS 可解析", msg)
