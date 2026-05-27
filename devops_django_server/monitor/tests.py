from django.test import SimpleTestCase

from monitor.dns_diagnosis import QTYPE_A, QTYPE_NS, classify_dns_evidence, normalize_hostname


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
