from aidr_obs.redact import REDACTED, redact, redact_str


def test_secret_shapes_are_masked():
    fake_aws = "AKIA" + "ABCDEFGHIJKLMNOP"
    text = (
        "curl -H 'Authorization: Bearer abc.def.ghi' https://bob:hunter2@example.com "
        "API_KEY=topsecret ghp_" + "a" * 30 + " sk-ant-" + "b" * 20 + " " + fake_aws
    )
    out = redact_str(text)
    for leaked in ("abc.def.ghi", "hunter2", "topsecret", "a" * 30, "b" * 20, fake_aws):
        assert leaked not in out
    assert REDACTED in out


def test_json_text_with_sensitive_key_is_masked():
    assert "s3cr3t" not in redact_str('{"api_key": "s3cr3t", "name": "ok"}')


def test_private_key_block_is_masked():
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3Nza\n-----END OPENSSH PRIVATE KEY-----"
    assert "AAAAB3Nza" not in redact_str("contents: " + pem)


def test_dict_keys_and_truncation():
    out = redact({"tool_input": {"password": "pw", "command": "ls"}, "big": "x" * 50}, limit=10)
    assert out["tool_input"] == {"password": REDACTED, "command": "ls"}
    assert out["big"].startswith("x" * 10) and "truncated 40 chars" in out["big"]


def test_token_counts_are_kept():
    out = redact(
        {
            "input_tokens": 1234,
            "output_tokens": 56.0,
            "cache_read_tokens": "789",
            "auth_token": "abc",
            "refresh_tokens": "a1b2",
            "has_token": True,
        }
    )
    assert out == {
        "input_tokens": 1234,
        "output_tokens": 56.0,
        "cache_read_tokens": "789",
        "auth_token": REDACTED,
        "refresh_tokens": REDACTED,
        "has_token": True,
    }


def test_plain_text_is_untouched():
    assert redact_str("ls -la /tmp && cat README.md") == "ls -la /tmp && cat README.md"
