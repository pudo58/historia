from ghm.security import SecretStore


def test_secret_store_round_trip() -> None:
    store = SecretStore("only-for-test")
    encrypted = store.encrypt("super-secret")
    assert "super-secret" not in encrypted
    assert store.decrypt(encrypted) == "super-secret"
