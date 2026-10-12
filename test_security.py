from unittest.mock import patch
import pytest
from main import assert_public_url

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/video.mp4",
    "http://10.0.0.2/video.mp4",
    "http://192.168.1.2/video.mp4",
    "http://localhost/video.mp4",
    "file:///etc/passwd",
    "http://user:pass@example.com/video.mp4",
])
def test_rejects_local_or_unsafe_urls(url):
    with pytest.raises(ValueError):
        assert_public_url(url)

def test_rejects_hostname_resolving_to_private_ip():
    fake=[(2,1,6,"",("127.0.0.1",80))]
    with patch("socket.getaddrinfo",return_value=fake):
        with pytest.raises(ValueError):
            assert_public_url("https://example.test/video")

def test_allows_hostname_with_public_resolution():
    fake=[(2,1,6,"",("93.184.216.34",443))]
    with patch("socket.getaddrinfo",return_value=fake):
        assert_public_url("https://example.test/video")
