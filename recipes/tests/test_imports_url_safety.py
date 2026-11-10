"""SSRF : l'import d'une recette (et de son image) ne va jamais chercher une adresse interne."""

import ipaddress
from unittest import mock

import pytest
import requests

from core import url_safety
from recipes.services import image_uploader, recipe_importer

DNS = {
    'blog.example': ['93.184.216.34'],
    'evil.example': ['93.184.216.35'],
    'rebind.example': ['93.184.216.36', '10.0.0.5'],
    'localhost': ['127.0.0.1'],
    'minio': ['172.18.0.4'],
}


@pytest.fixture(autouse=True)
def fake_dns():
    def resolve(host, port):
        try:
            return [ipaddress.ip_address(host)]
        except ValueError:
            if host not in DNS:
                raise OSError('unknown host') from None
            return [ipaddress.ip_address(ip) for ip in DNS[host]]

    with mock.patch.object(url_safety, '_resolved_ips', side_effect=resolve):
        yield


def _response(status=200, location=None, body='<html></html>'):
    resp = requests.Response()
    resp.status_code = status
    resp._content = body.encode()
    resp._content_consumed = True
    if location:
        resp.headers['Location'] = location
    return resp


@pytest.mark.parametrize(
    'url',
    [
        'http://169.254.169.254/latest/meta-data/',
        'http://127.0.0.1:8000/admin/',
        'http://localhost/',
        'http://minio:9000/bucket',
        'http://[::ffff:127.0.0.1]/',
        'http://10.1.2.3/',
        'http://rebind.example/',
        'file:///etc/passwd',
        'gopher://blog.example/',
        'http://unknown.invalid/',
    ],
)
def test_internal_or_odd_urls_are_refused_without_any_request(url):
    assert not url_safety.is_public_url(url)
    with mock.patch.object(url_safety.requests, 'get') as get:
        with pytest.raises(url_safety.UnsafeURLError):
            url_safety.safe_get(url, timeout=1)
        assert recipe_importer.extract_generic_recipe(url) is None
        assert image_uploader.download_and_upload_image(url, user_id=1) is None
    get.assert_not_called()


def test_redirect_to_an_internal_address_is_not_followed():
    hops = [_response(302, location='http://169.254.169.254/latest/meta-data/')]
    with mock.patch.object(url_safety.requests, 'get', side_effect=hops) as get:
        with pytest.raises(url_safety.UnsafeURLError):
            url_safety.safe_get('https://evil.example/recette', timeout=1)
    assert [c.args[0] for c in get.call_args_list] == ['https://evil.example/recette']
    assert all(c.kwargs['allow_redirects'] is False for c in get.call_args_list)


def test_public_url_and_public_redirect_are_fetched():
    hops = [_response(301, location='/recette-2'), _response(200)]
    with mock.patch.object(url_safety.requests, 'get', side_effect=hops) as get:
        resp = url_safety.safe_get('https://blog.example/recette', timeout=1)
    assert resp.status_code == 200
    assert [c.args[0] for c in get.call_args_list] == ['https://blog.example/recette', 'https://blog.example/recette-2']
