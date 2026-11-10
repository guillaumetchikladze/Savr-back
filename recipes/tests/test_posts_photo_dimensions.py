"""Posts : taille d'affichage des photos (`PostPhoto.width` / `height`) → cadre du post au ratio de la 1re photo."""

from importlib import import_module
from io import BytesIO, StringIO
from unittest import mock

import pytest
import requests
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from PIL import Image

from recipes.models import Post, PostPhoto
from recipes.services.photo_dimensions import (
    dimensions_from_chunks,
    dimensions_from_file,
    fetch_image_dimensions,
)
from recipes.tasks import fill_post_photo_dimensions

pytestmark = pytest.mark.django_db

TASK_NAME = 'recipes.tasks.fill_post_photo_dimensions'


def _image_bytes(width, height, fmt='JPEG', orientation=None):
    image = Image.new('RGB', (width, height), (200, 120, 80))
    buf = BytesIO()
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        image.save(buf, fmt, exif=exif.tobytes())
    else:
        image.save(buf, fmt)
    return buf.getvalue()


def _chunks(data, size=512):
    return (data[i : i + size] for i in range(0, len(data), size))


class _FakeResponse:
    def __init__(self, data=b'', status_code=200):
        self.status_code = status_code
        self._data = data
        self.closed = False

    def iter_content(self, chunk_size):
        return _chunks(self._data, chunk_size)

    def close(self):
        self.closed = True


def _cooked_post(client, make_recipe, user):
    resp = client.post(
        '/api/posts/cooked/',
        {'recipe_id': make_recipe(user).id, 'cooked_at': '2026-11-08T20:15:00+01:00'},
        format='json',
    )
    assert resp.status_code == 201, resp.data
    return Post.objects.get(id=resp.data['id'])


def _photo(post=None, path='posts/1/a.jpg', **extra):
    return PostPhoto.objects.create(post=post, photo_type='at_meal_time', image_path=path, **extra)


# ---------------------------------------------------------------------------
# Lecture de la taille (en-tête seulement, orientation EXIF)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ('data', 'expected'),
    [
        (_image_bytes(1600, 900), (1600, 900)),
        (_image_bytes(300, 500, 'PNG'), (300, 500)),
        (_image_bytes(800, 600, orientation=6), (600, 800)),  # photo tournée de 90° : affichée en portrait
        (_image_bytes(800, 600, orientation=3), (800, 600)),  # 180° : même cadre
        (b'not an image at all' * 50, None),
        (b'', None),
    ],
)
def test_dimensions_from_stream_and_file(data, expected):
    assert dimensions_from_chunks(_chunks(data)) == expected
    upload = BytesIO(data)
    assert dimensions_from_file(upload) == expected
    assert upload.tell() == 0  # curseur remis au début pour l'envoi S3 qui suit


def test_dimensions_read_only_the_header():
    data = _image_bytes(4000, 3000)
    fed = []

    def chunks():
        for chunk in _chunks(data, 1024):
            fed.append(chunk)
            yield chunk

    assert dimensions_from_chunks(chunks()) == (4000, 3000)
    assert sum(len(c) for c in fed) < len(data)


def test_fetch_image_dimensions_robust_to_missing_or_unreachable_file():
    ok = _FakeResponse(_image_bytes(1080, 1350))
    with mock.patch('requests.get', return_value=ok) as get:
        assert fetch_image_dimensions('meal_plans/1/a.jpg') == (1080, 1350)
    assert get.call_args.kwargs['stream'] is True and 'meal_plans/1/a.jpg' in get.call_args.args[0]
    assert ok.closed

    with mock.patch('requests.get', return_value=_FakeResponse(status_code=404)):
        assert fetch_image_dimensions('meal_plans/1/missing.jpg') is None
    with mock.patch('requests.get', side_effect=requests.ConnectionError('down')):
        assert fetch_image_dimensions('meal_plans/1/a.jpg') is None
    with mock.patch('requests.get', return_value=_FakeResponse(b'<html>error</html>')):
        assert fetch_image_dimensions('meal_plans/1/a.jpg') is None
    assert fetch_image_dimensions('') is None


# ---------------------------------------------------------------------------
# Remplissage à l'upload
# ---------------------------------------------------------------------------
def test_fill_task_sizes_only_photos_without_size():
    unsized = _photo(path='posts/1/a.jpg')
    sized = _photo(path='posts/1/b.jpg', width=10, height=20)

    with mock.patch('requests.get', return_value=_FakeResponse(_image_bytes(1200, 800))):
        assert fill_post_photo_dimensions([unsized.id, sized.id]) == 1

    unsized.refresh_from_db()
    sized.refresh_from_db()
    assert (unsized.width, unsized.height) == (1200, 800)
    assert (sized.width, sized.height) == (10, 20)


def test_direct_s3_upload_schedules_size_read_after_commit(
    api, user, make_meal_plan, celery_delay, django_capture_on_commit_callbacks
):
    client = api.as_user(user)
    mp = make_meal_plan(user)

    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(
            '/api/posts/confirm_photo_upload/',
            {'meal_plan_id': mp.id, 'image_path': f'meal_plans/{mp.id}/a.jpg', 'photo_type': 'at_meal_time'},
            format='json',
        )

    assert resp.status_code == 201, resp.data
    assert resp.data['width'] is None and resp.data['height'] is None
    assert (TASK_NAME, ([resp.data['id']],), {}) in celery_delay.calls


def test_add_photos_by_path_schedules_size_read(
    api, user, make_recipe, celery_delay, django_capture_on_commit_callbacks
):
    client = api.as_user(user)
    post = _cooked_post(client, make_recipe, user)

    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(
            f'/api/posts/{post.id}/photos/', {'image_paths': [f'meal_plans/{post.meal_plan_id}/a.jpg']}, format='json'
        )

    assert resp.status_code == 201, resp.data
    photo_id = resp.data['photos'][0]['id']
    assert any(name == TASK_NAME and photo_id in args[0] for name, args, _ in celery_delay.calls)


def test_multipart_upload_reads_size_from_the_file(api, user, make_recipe):
    client = api.as_user(user)
    post = _cooked_post(client, make_recipe, user)
    s3 = mock.MagicMock()
    positions = []
    s3.upload_fileobj.side_effect = lambda fileobj, *a, **k: positions.append(fileobj.tell())
    upload = SimpleUploadedFile('dish.jpg', _image_bytes(900, 1200), content_type='image/jpeg')

    with mock.patch('recipes.views.build_s3_client', return_value=s3):
        resp = client.post(f'/api/posts/{post.id}/upload_photo/', {'photo': upload}, format='multipart')

    assert resp.status_code == 201, resp.data
    assert (resp.data['width'], resp.data['height']) == (900, 1200)
    assert positions == [0]  # le fichier part entier vers S3 après la lecture de l'en-tête


# ---------------------------------------------------------------------------
# Exposition dans les posts (feed + détail)
# ---------------------------------------------------------------------------
def test_post_serializers_expose_photo_size(api, user, make_recipe):
    client = api.as_user(user)
    post = _cooked_post(client, make_recipe, user)
    _photo(post, path='posts/1/a.jpg', width=1080, height=1350, order=1)
    _photo(post, path='posts/1/b.jpg', order=2)

    feed = client.get('/api/posts/', {'is_published': 'true'}).data['results']
    detail = client.get(f'/api/posts/{post.id}/').data

    entry = next(p for p in feed if p['id'] == post.id)
    for photos in (entry['photos'], detail['photos']):
        assert [(p['width'], p['height']) for p in photos] == [(1080, 1350), (None, None)]


# ---------------------------------------------------------------------------
# Backfill des photos existantes
# ---------------------------------------------------------------------------
def test_backfill_command_is_robust_and_idempotent(api, user, make_recipe):
    post = _cooked_post(api.as_user(user), make_recipe, user)
    ok = _photo(post, path='posts/1/ok.jpg')
    missing = _photo(post, path='posts/1/missing.jpg')
    unreachable = _photo(post, path='posts/1/down.jpg')
    already = _photo(post, path='posts/1/sized.jpg', width=5, height=7)
    meal_only = _photo(None, path='meal_plans/1/x.jpg')

    def fake_get(url, **kwargs):
        if 'missing' in url:
            return _FakeResponse(status_code=404)
        if 'down' in url:
            raise requests.ConnectionError('timeout')
        return _FakeResponse(_image_bytes(1600, 900))

    with mock.patch('requests.get', side_effect=fake_get):
        dry = StringIO()
        call_command('backfill_post_photo_dimensions', '--dry-run', stdout=dry)
        assert not PostPhoto.objects.filter(width__isnull=False).exclude(id=already.id).exists()
        assert '1 photos lisibles' in dry.getvalue()

        out = StringIO()
        call_command('backfill_post_photo_dimensions', stdout=out)
        call_command('backfill_post_photo_dimensions', '--all-photos', stdout=StringIO())

    sizes = {p.id: (p.width, p.height) for p in PostPhoto.objects.all()}
    assert sizes[ok.id] == (1600, 900)
    assert sizes[missing.id] == (None, None)
    assert sizes[unreachable.id] == (None, None)
    assert sizes[already.id] == (5, 7)
    assert sizes[meal_only.id] == (1600, 900)  # seulement avec --all-photos
    assert '1 photos dimensionnées, 2 échecs sur 3' in out.getvalue()


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------
def test_migration_adds_nullable_size_fields():
    migration = import_module('recipes.migrations.0085_post_photo_dimensions').Migration
    added = {op.name: op.field for op in migration.operations}
    assert set(added) == {'width', 'height'}
    assert all(field.null and field.blank for field in added.values())
    call_command('makemigrations', 'recipes', '--check', '--dry-run', stdout=StringIO())


# ---------------------------------------------------------------------------
# Taille envoyée par l'app (photo redimensionnée avant l'envoi direct S3)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ('width', 'height', 'expected'),
    [
        (1080, 1440, (1080, 1440)),
        ('1440', '1080', (1440, 1080)),
        (1080.0, 1350.0, (1080, 1350)),
        (None, 1080, None),
        (0, 1080, None),
        (-5, 1080, None),
        (True, 1080, None),
        (1080.5, 1080, None),
        ('abc', 1080, None),
        (99999, 1080, None),
    ],
)
def test_client_photo_size_validation(width, height, expected):
    from recipes.services.photo_dimensions import client_photo_size

    assert client_photo_size(width, height) == expected


def test_confirm_upload_with_client_size_skips_the_task(
    api, user, make_meal_plan, celery_delay, django_capture_on_commit_callbacks
):
    client = api.as_user(user)
    mp = make_meal_plan(user)

    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(
            '/api/posts/confirm_photo_upload/',
            {
                'meal_plan_id': mp.id,
                'image_path': f'meal_plans/{mp.id}/a.jpg',
                'photo_type': 'at_meal_time',
                'width': 1080,
                'height': 1440,
            },
            format='json',
        )

    assert resp.status_code == 201, resp.data
    assert (resp.data['width'], resp.data['height']) == (1080, 1440)
    assert not any(name == TASK_NAME for name, _, _ in celery_delay.calls)

    with django_capture_on_commit_callbacks(execute=True):
        bad = client.post(
            '/api/posts/confirm_photo_upload/',
            {
                'meal_plan_id': mp.id,
                'image_path': f'meal_plans/{mp.id}/b.jpg',
                'photo_type': 'at_meal_time',
                'width': 'nope',
                'height': 1440,
            },
            format='json',
        )
    assert bad.status_code == 201, bad.data
    assert bad.data['width'] is None and bad.data['height'] is None
    assert (TASK_NAME, ([bad.data['id']],), {}) in celery_delay.calls


def test_add_photos_by_path_with_client_sizes(api, user, make_recipe, celery_delay, django_capture_on_commit_callbacks):
    client = api.as_user(user)
    post = _cooked_post(client, make_recipe, user)
    base = f'meal_plans/{post.meal_plan_id}'

    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(
            f'/api/posts/{post.id}/photos/',
            {
                'image_paths': [f'{base}/a.jpg', f'{base}/b.jpg', f'{base}/c.jpg'],
                'image_sizes': [{'width': 1440, 'height': 1080}, None],  # c sans taille : liste plus courte
            },
            format='json',
        )

    assert resp.status_code == 201, resp.data
    photos = resp.data['photos']
    assert [(p['width'], p['height']) for p in photos] == [(1440, 1080), (None, None), (None, None)]
    scheduled = [args[0] for name, args, _ in celery_delay.calls if name == TASK_NAME]
    assert scheduled == [[photos[1]['id'], photos[2]['id']]]
