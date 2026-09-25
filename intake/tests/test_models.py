from django.db import IntegrityError, transaction
from django.test import TestCase

from intake.models import InboundAttachment, InboundEmail


class IntakeModelsTest(TestCase):
    def test_idempotent_by_message_id_and_sha256(self):
        e = InboundEmail.objects.create(message_id='<a@b>', sender='tc@channel.lk')
        with self.assertRaises(IntegrityError), transaction.atomic():
            InboundEmail.objects.create(message_id='<a@b>', sender='tc@channel.lk')
        InboundAttachment.objects.create(email=e, filename='tc.xlsx', sha256='f' * 64)
        with self.assertRaises(IntegrityError), transaction.atomic():
            InboundAttachment.objects.create(email=e, filename='copy.xlsx', sha256='f' * 64)
