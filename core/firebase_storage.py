"""Firebase Storage backend for Django FileField.

Drop-in replacement for the default local-filesystem storage. Activated by setting
FIREBASE_STORAGE_BUCKET in environment variables and DEFAULT_FILE_STORAGE in settings.

Files are stored at gs://<bucket>/<name> and served via 1-hour signed URLs.
"""
import io
from datetime import timedelta

from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible


@deconstructible
class FirebaseStorage(Storage):
    """Store uploaded files in Firebase Storage (Google Cloud Storage bucket)."""

    def __init__(self):
        self._bucket_obj = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_bucket(self):
        if self._bucket_obj is None:
            try:
                import firebase_admin
                from firebase_admin import storage as fb_storage
                # Ensure the default app is initialised (done in core/apps.py)
                if not firebase_admin._apps:
                    raise RuntimeError(
                        'Firebase Admin SDK not initialised. '
                        'Ensure FIREBASE_STORAGE_BUCKET is set and CoreConfig.ready() ran.'
                    )
                self._bucket_obj = fb_storage.bucket()
            except ImportError as exc:
                raise ImportError(
                    'firebase-admin is required for FirebaseStorage. '
                    'Run: pip install firebase-admin'
                ) from exc
        return self._bucket_obj

    def _blob(self, name):
        return self._get_bucket().blob(name)

    # ------------------------------------------------------------------
    # Storage API
    # ------------------------------------------------------------------

    def _save(self, name, content):
        blob = self._blob(name)
        content.seek(0)
        blob.upload_from_file(content, content_type='application/octet-stream')
        return name

    def _open(self, name, mode='rb'):
        blob = self._blob(name)
        buf = io.BytesIO()
        blob.download_to_file(buf)
        buf.seek(0)
        return buf

    def exists(self, name):
        return self._blob(name).exists()

    def delete(self, name):
        blob = self._blob(name)
        if blob.exists():
            blob.delete()

    def url(self, name):
        """Return a signed download URL valid for 4 hours.

        Uses v2 signing which works with a service-account private key on any
        host (no GCP infrastructure or extra IAM roles required).
        """
        blob = self._blob(name)
        return blob.generate_signed_url(
            expiration=timedelta(hours=4),
            method='GET',
            version='v2',
        )

    def size(self, name):
        blob = self._blob(name)
        blob.reload()
        return blob.size

    def listdir(self, path):
        bucket = self._get_bucket()
        blobs = list(bucket.list_blobs(prefix=path))
        dirs, files = [], []
        for blob in blobs:
            rel = blob.name[len(path):].lstrip('/')
            if '/' in rel:
                d = rel.split('/')[0]
                if d not in dirs:
                    dirs.append(d)
            else:
                files.append(rel)
        return dirs, files
