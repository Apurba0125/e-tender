# e-Tender Management System (Django)

Implements the SRS v1.0: notice → tender → sealed vendor bids → manual opening → Technical → Finance →
3-vendor comparative statement + validation → CFO → auto-generated award email.

## Run
```bash
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo            # demo users (password: Tender@12345 – change it!)
python manage.py runserver
python manage.py run_scheduler --loop # 2nd terminal: closes bidding at deadline + reminders
python manage.py test tenders         # end-to-end tests
```
* Internal portal: http://127.0.0.1:8000/internal/login/ — `admin`, `po`, `tc1`, `tc2`, `fc1`, `fc2`, `cfo`
* Vendor portal: http://127.0.0.1:8000/vendor/login/ — `vendor1` … `vendor4` (or self-register)
* Django admin (`/admin/`): create users/roles (committees = users with role TC / FC), edit **Config** (approval mode ANY/MAJORITY/ALL, policy switches).
* Admin account email is `apurbasarkar17072002@gmail.com`; it is also the default "From" address.

## Email
The app loads environment variables from `.env` in the project root; `.env.example` is only a reference and is not loaded. Without SMTP credentials, the console backend does not send real mail and award emails are marked as failed. To send via Gmail, create `.env` with these entries, using a Google App Password rather than your account password:
```dotenv
EMAIL_HOST_USER=your-account@gmail.com
EMAIL_HOST_PASSWORD=your-google-app-password
DEFAULT_FROM_EMAIL=your-account@gmail.com
```
The `.env` file is ignored by Git. Restart the server after creating or changing it. Django defaults to Gmail SMTP on port 587 with TLS; see `.env.example` for additional settings.

## Where the SRS is covered
| SRS area | Code |
|---|---|
| Roles/RBAC, lockout, 20-min session, separate portals | `accounts/`, `tenders/decorators.py`, settings |
| Vendor registration, email verify, approve/suspend/blacklist | `accounts/views.py` |
| Notices, tenders (open/limited), corrigendum, extend, cancel, clone | `tenders/views.py`, `services.py` |
| Sealed bids (Fernet, per-tender key), versioning, receipts, server-time deadline | `crypto.py`, `BidVersion`, `services.submit_bid` |
| Auto close, manual open, reminders, SLA | `run_scheduler`, `services.open_tender` |
| TC → FC → statement (L1–L3) → validation → CFO, strict sequence, send-back/reject | `services.py` |
| Award email + preview, retry, regret mails, vendor acceptance | `services.send_award_email`, `templates/emails/` |
| Audit (append-only, hash-chained), notifications, reports/CSV | `AuditLog`, `Notification`, `reports` view |

## Deviations / not included (decisions for you)
* Added status `TECHNICAL_APPROVED` (PO must forward to Finance, per SRS step 5).
* Top 3 = lowest **price + taxes** among technically qualified bids (SRS open question #2).
* Not built: 2FA (AUTH-03), tender Q&A (TEN-08), two-person opening (OPN-06), CFO delegation (APR-33), SMS, antivirus scan, PDF export (CSV provided), async queue (emails are sent inline and logged; swap `deliver_email` for Celery if needed).
* Uploaded files live in `media/` (private, served only via permission-checked views); add disk/S3 encryption for production.
* SQLite by default; switch `DATABASES` to PostgreSQL for production and set `DJANGO_DEBUG=0`.
