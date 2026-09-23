from edgechaindb.validation import _effective_profile


def test_validation_profile_alias_is_backwards_compatible():
    assert _effective_profile("paper") == "full"
    assert _effective_profile("full") == "full"
    assert _effective_profile("smoke") == "smoke"
