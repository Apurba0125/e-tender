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


class AdminUserManagementTests(TestCase):
	def test_admin_can_create_internal_user(self):
		admin = User.objects.create_user(
			username='admin-user', email='admin@example.com', password='Test-password-123',
			role=User.Role.ADMIN,
		)
		self.client.force_login(admin)
		page = self.client.get(reverse('user_admin'))
		self.assertContains(page, 'Create internal user')

		response = self.client.post(reverse('user_admin'), {
			'username': 'reviewer',
			'email': 'reviewer@example.com',
			'first_name': 'Taylor',
			'last_name': 'Reviewer',
			'role': User.Role.TC,
			'phone': '1234567890',
			'password1': '123',
			'password2': '123',
		})

		created_user = User.objects.get(username='reviewer')
		self.assertRedirects(response, reverse('user_admin'))
		self.assertEqual(created_user.role, User.Role.TC)
		self.assertTrue(created_user.check_password('123'))

	def test_non_admin_cannot_access_user_management(self):
		user = User.objects.create_user(
			username='officer', email='officer@example.com', password='Test-password-123',
			role=User.Role.PO,
		)
		self.client.force_login(user)

		response = self.client.get(reverse('user_admin'))

		self.assertEqual(response.status_code, 403)
