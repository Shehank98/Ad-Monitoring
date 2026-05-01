import os
import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        """Initialise Firebase Admin SDK if FIREBASE_STORAGE_BUCKET is configured."""
        bucket = os.environ.get('FIREBASE_STORAGE_BUCKET', '')
        if not bucket:
            return

        try:
            import firebase_admin
            from firebase_admin import credentials

            if firebase_admin._apps:
                # Already initialised (e.g. by the legacy Streamlit layer); nothing to do.
                return

            cred_dict = {
                'type':                        os.environ.get('FIREBASE_TYPE', 'service_account'),
                'project_id':                  os.environ.get('FIREBASE_PROJECT_ID', ''),
                'private_key_id':              os.environ.get('FIREBASE_PRIVATE_KEY_ID', ''),
                'private_key':                 os.environ.get('FIREBASE_PRIVATE_KEY', '').replace('\\n', '\n'),
                'client_email':                os.environ.get('FIREBASE_CLIENT_EMAIL', ''),
                'client_id':                   os.environ.get('FIREBASE_CLIENT_ID', ''),
                'auth_uri':                    os.environ.get('FIREBASE_AUTH_URI',
                                                              'https://accounts.google.com/o/oauth2/auth'),
                'token_uri':                   os.environ.get('FIREBASE_TOKEN_URI',
                                                              'https://oauth2.googleapis.com/token'),
                'auth_provider_x509_cert_url': os.environ.get(
                    'FIREBASE_AUTH_PROVIDER_X509_CERT_URL',
                    'https://www.googleapis.com/oauth2/v1/certs',
                ),
                'client_x509_cert_url':        os.environ.get('FIREBASE_CLIENT_X509_CERT_URL', ''),
            }

            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred, {'storageBucket': bucket})
            logger.info('Firebase Admin SDK initialised with bucket: %s', bucket)

        except ImportError:
            logger.warning(
                'firebase-admin is not installed. Firebase Storage will not be available. '
                'Run: pip install firebase-admin'
            )
        except Exception:
            logger.exception('Failed to initialise Firebase Admin SDK.')
