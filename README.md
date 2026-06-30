# RFQ Workflow — Streamlit Edition

A complete RFQ workflow management system built in pure Python with Streamlit.

## Features
- User authentication with multi-role support (Admin, Man Days Estimator, IRM Estimator, Price Owner, Approver)
- RFQ lifecycle: New → In Estimation → Pricing → Pending Approval → Approved → Submitted → Closed
- Status-driven automation: emails sent on every transition; auto-transitions on file uploads
- File uploads to MongoDB GridFS (BoM, Man-Days, IRM, Contract)
- Gmail SMTP email notifications (configurable from inside the app)
- KPI dashboard, activity log, PDF proposal summary
- SLA timers (priority-based due dates)

## Prerequisites
- Python 3.10+
- MongoDB running locally (or remote)
  - Docker: `docker run -d -p 27017:27017 --name mongo mongo:7`
  - Or install [MongoDB Community](https://www.mongodb.com/try/download/community)

## Setup

```bash
cd streamlit_rfq
python -m venv venv
source venv/bin/activate              # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                  # then edit if needed
```

## Run

```bash
streamlit run app.py
```

Open http://localhost:8501 in your browser.

## Default Login (auto-seeded on first run)

| Email | Password | Roles |
|---|---|---|
| admin@rfq.com | admin123 | Admin |
| mandays@rfq.com | password123 | Man Days Estimator |
| irm@rfq.com | password123 | IRM Estimator |
| price@rfq.com | password123 | Price Owner |
| approver@rfq.com | password123 | Approver |

## Gmail SMTP (optional)
After logging in as admin, open **Email Settings** in the sidebar and enter your Gmail address + 16-character App Password.
- Generate an App Password at https://myaccount.google.com/apppasswords (requires 2-Step Verification).
- Until configured, emails are logged to the console and stored in `db.email_log` (status: `logged_only`).

## File structure
```
streamlit_rfq/
├── app.py              # Main entry — sidebar, routing
├── services.py         # DB, auth, email, PDF helpers
├── views.py            # Page renderers (dashboard, RFQ list/create/detail, users, settings)
├── requirements.txt
├── .env.example
└── README.md
```
