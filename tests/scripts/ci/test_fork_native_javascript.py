"""Native skip, empty-report and missing-file results cannot authorize merging."""
import pytest

from scripts.ci.fork_native_javascript import summarize_report


def report(status="passed", title="native operation"):
    return {"success": True, "numFailedTests": 0, "testResults": [{
        "name": "G:/candidate/electron/native.test.ts", "status": "passed",
        "assertionResults": [{"fullName": title, "status": status}],
    }]}


def test_real_native_assertion_must_pass():
    assert summarize_report(report(), {"electron/native.test.ts": ["native operation"]})["passed"] == 1


@pytest.mark.parametrize("result", [
    {}, {"success": True, "testResults": []}, report("pending"), report("failed"),
    report(title="different test"),
])
def test_incomplete_or_skipped_native_proof_refuses(result):
    with pytest.raises(ValueError):
        summarize_report(result, {"electron/native.test.ts": ["native operation"]})


def test_missing_required_file_refuses():
    with pytest.raises(ValueError):
        summarize_report(report(), {"electron/absent.test.ts": ["native operation"]})
