"""
Tests for app/services/redact.py — secret redaction applied to execute_tool's
logging (the busiest logging chokepoint in the app, hit by every tool call).
"""
from app import db
from app.models.logs import AuditLog
from app.services.redact import redact_args, redact_text, redact_value


class TestRedactText:
    # These fixtures are built via concatenation rather than as literal
    # strings — several read as real credentials to secret scanners
    # (GitHub push protection blocked the first version of this file over
    # the Slack token literal), even though they're just test data for the
    # redaction regexes.

    def test_redacts_bearer_token(self):
        fake_token = "Bearer " + "abcdef0123456789ABCDEF"
        out = redact_text("Authorization: " + fake_token)
        assert fake_token not in out
        assert "[REDACTED:bearer_token]" in out

    def test_redacts_jwt(self):
        jwt = ".".join([
            "eyJhbGciOiJIUzI1NiJ9",
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        ])
        out = redact_text(f"token={jwt}")
        assert jwt not in out
        assert "[REDACTED:jwt]" in out

    def test_redacts_github_token(self):
        fake_token = "ghp_" + "1234567890abcdefghijklmnopqrstuv"
        out = redact_text(f"using {fake_token} for auth")
        assert fake_token not in out
        assert "[REDACTED:github_token]" in out

    def test_redacts_openai_key(self):
        fake_key = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
        out = redact_text(f"key: {fake_key}")
        assert fake_key not in out
        assert "[REDACTED:openai_key]" in out

    def test_redacts_slack_token(self):
        # Built via concatenation, not a literal — a contiguous xoxb-...
        # string here reads as a real Slack token to secret scanners
        # (GitHub's push protection flagged the literal form).
        fake_slack_token = "xoxb-" + "1234567890" + "-" + "abcdefghijklmnop"
        out = redact_text(fake_slack_token)
        assert fake_slack_token not in out
        assert "[REDACTED:slack_token]" in out

    def test_redacts_aws_access_key(self):
        out = redact_text("AKIAIOSFODNN7EXAMPLE is the access key")
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "[REDACTED:aws_access_key]" in out

    def test_redacts_fernet_token(self):
        fake_fernet = "gAAAAABkX1Y2Z3" + "a" * 30
        out = redact_text(f"stored={fake_fernet}")
        assert fake_fernet not in out
        assert "[REDACTED:fernet_token]" in out

    def test_redacts_private_key_block(self):
        block = "-----BEGIN RSA PRIVATE KEY-----\nMIIBogIBAAJ...\n-----END RSA PRIVATE KEY-----"
        out = redact_text(f"contents: {block}")
        assert "MIIBogIBAAJ" not in out
        assert "[REDACTED:private_key_block]" in out

    def test_leaves_ordinary_text_untouched(self):
        text = "read_file returned: hello world, this is normal file content with no secrets."
        assert redact_text(text) == text

    def test_handles_empty_and_none(self):
        assert redact_text("") == ""
        assert redact_text(None) is None


class TestRedactValue:
    def test_masks_sensitive_field_name_regardless_of_value_shape(self):
        # Even a value that doesn't match any known secret pattern must be
        # masked outright when the field name itself says it's sensitive.
        assert redact_value("password", "hunter2") == "[REDACTED]"
        assert redact_value("api_key", "not-a-recognized-shape") == "[REDACTED]"
        assert redact_value("Authorization", "whatever") == "[REDACTED]"

    def test_field_name_matching_is_case_and_punctuation_insensitive(self):
        assert redact_value("API_KEY", "x") == "[REDACTED]"
        assert redact_value("apiKey", "x") == "[REDACTED]"
        assert redact_value("Api-Key", "x") == "[REDACTED]"

    def test_non_sensitive_field_name_still_pattern_scanned(self):
        out = redact_value("notes", "my key is sk-abcdefghijklmnopqrstuvwxyz123456")
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in out

    def test_non_string_values_pass_through(self):
        assert redact_value("count", 42) == 42
        assert redact_value("enabled", True) is True
        assert redact_value("data", None) is None


class TestRedactArgs:
    def test_redacts_sensitive_keys_in_a_tool_args_dict(self):
        args = {"command": "echo hi", "api_key": "sk-abcdefghijklmnopqrstuvwxyz123456"}
        out = redact_args(args)
        assert out["command"] == "echo hi"
        assert out["api_key"] == "[REDACTED]"

    def test_pattern_scans_non_sensitive_string_values(self):
        args = {"content": "Bearer abcdef0123456789ABCDEF is my token"}
        out = redact_args(args)
        assert "abcdef0123456789ABCDEF" not in out["content"]

    def test_non_dict_input_passed_through(self):
        assert redact_args(None) is None
        assert redact_args("not a dict") == "not a dict"


class TestLogAuditRedactsPersistedRows:
    """_log_audit persists input_params/result/error straight to AuditLog,
    which is queryable indefinitely via the in-app Logs viewer — a more
    exposed, longer-lived sink than a rotating log file. A secret that
    slipped through into a tool's raw result text (e.g. read_file returning
    the contents of a file containing an API key) must not survive into
    the persisted row."""

    def test_secret_in_result_is_redacted_before_persisting(self, app):
        from app.services.mcp.tools import _log_audit

        secret_result = "file contents: sk-abcdefghijklmnopqrstuvwxyz123456"
        with app.app_context():
            _log_audit(
                tool_name="read_file", tier=0, caller="TEST\\User",
                session_id=None, run_id=None,
                input_params='{"path": "/tmp/secrets.txt"}',
                result=secret_result,
            )
            entry = AuditLog.query.filter_by(tool_name="read_file").order_by(
                AuditLog.created_at.desc()).first()
            assert entry is not None
            assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in entry.result_summary
            assert "[REDACTED:openai_key]" in entry.result_summary

    def test_secret_in_error_is_redacted_before_persisting(self, app):
        from app.services.mcp.tools import _log_audit

        with app.app_context():
            _log_audit(
                tool_name="call_connector", tier=1, caller="TEST\\User",
                session_id=None, run_id=None,
                input_params="{}",
                result="Error: request failed",
                error="upstream rejected Bearer abcdef0123456789ABCDEF",
            )
            entry = AuditLog.query.filter_by(tool_name="call_connector").order_by(
                AuditLog.created_at.desc()).first()
            assert entry is not None
            assert "abcdef0123456789ABCDEF" not in entry.error
            assert "[REDACTED:bearer_token]" in entry.error
