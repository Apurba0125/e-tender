from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import User


class LoginLockoutTests(TestCase):
	def test_locked_account_displays_lockout_message(self):
		User.objects.create_user(
			username='locked-user',
			email='locked@example.com',
			password='Test-password-123',
			role=User.Role.PO,
			locked_until=timezone.now() + timedelta(minutes=15),
		)

		response = self.client.post(reverse('internal_login'), {
			'username': 'locked-user',
			'password': 'Test-password-123',
		})

		self.assertContains(
			response,
			'Account temporarily locked after repeated failures. Try again later.',
		)
