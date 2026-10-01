from datetime import timedelta

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import User, Vendor


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


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class VendorApprovalEmailTests(TestCase):
	def test_first_approval_emails_vendor_verification_confirmation(self):
		admin = User.objects.create_user(
			username='admin-user', email='admin@example.com', password='Test-password-123',
			role=User.Role.ADMIN,
		)
		vendor_user = User.objects.create_user(
			username='vendor@example.com', email='vendor@example.com', password='Test-password-123',
			role=User.Role.VENDOR,
		)
		vendor = Vendor.objects.create(
			user=vendor_user, company_name='Acme Supplies', contact_name='Jordan Vendor',
			phone='1234567890', address='1 Market Street', tax_id='TAX-123', pan='PAN-123',
			category='Office supplies',
		)
		self.client.force_login(admin)

		response = self.client.post(reverse('vendor_action', args=[vendor.pk]), {'action': 'approve'})

		self.assertRedirects(response, reverse('vendor_list'))
		self.assertEqual(len(mail.outbox), 1)
		self.assertEqual(mail.outbox[0].to, ['vendor@example.com'])
		self.assertIn('Acme Supplies', mail.outbox[0].subject)
		self.assertIn('Jordan Vendor', mail.outbox[0].body)
		self.assertIn('verification of Acme Supplies is complete', mail.outbox[0].body)
		self.assertEqual(mail.outbox[0].from_email, 'apurbasarkar17072002@gmail.com')

		self.client.post(reverse('vendor_action', args=[vendor.pk]), {'action': 'approve'})
		self.assertEqual(len(mail.outbox), 1)
