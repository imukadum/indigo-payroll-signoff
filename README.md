# Indigo Cash Payroll Sign-Off

A small, independent web app so an employee can confirm (or dispute) a cash
payroll amount by tapping a link on their own phone — no login, no app
install, no account needed on their end. You (the manager/owner) get a
simple passcode-protected dashboard to enter amounts, send links, and see
who has responded.

This is v1 of Module 2 from the "Indigo Operations" spec. It runs on its
own — it does not require the Catering Orders module, which is still just
a written spec.

## What it does

1. You log in with a passcode.
2. You add employees (name + phone number).
3. For each cash payment, you type in the employee, the week, the pay
   date, and the amount. The app creates a one-time confirmation link.
4. You tap "Tap to text this link" to send it from **your own phone**
   (via a normal text message — see "About the text messages" below), or
   just copy the link.
5. The employee opens the link on their phone (no login) and taps "Yes,
   this is correct" or opens "No, this isn't right" and types what they
   actually received.
6. Your dashboard updates immediately: confirmed entries turn green,
   disputes turn red with what the employee said.
7. Once a link has been used, it's locked — visiting it again just shows
   the recorded response, so it can't be resubmitted or reused.
8. "Download CSV" gives you a spreadsheet of every entry (employee, week,
   amount entered, employee's response, timestamps, resolution notes) —
   this is the file meant to feed into next month's financial workbook.

## About the text messages (important limitation)

This app does **not** send text messages automatically — that requires a
paid SMS provider account (like Twilio) set up under your business, with
its own billing and verification, which isn't something I can set up on
your behalf. Instead, v1 uses a simpler approach: the "Tap to text this
link" button opens **your own phone's** normal messaging app with the
message pre-filled, and you tap send yourself. It's one extra tap, but it
means zero extra accounts or costs to get started.

If down the road you want it to send automatically without you tapping
send, that's a future upgrade — it would mean signing up for a service
like Twilio and giving me (or whoever maintains this) an API key to plug
in. The app is structured so that would be a small, contained change.

## Running it locally (to try it out)

You'll need Python 3 installed. From this folder:

```
pip install -r requirements.txt
python3 app.py
```

Then open `http://localhost:5055` in a browser. The default passcode is
`indigo2026` — **change this before using it for real** (see below).

## Setting your passcode (do this before real use)

By default the passcode is `indigo2026`, which anyone could guess. Set
your own by setting an environment variable before starting the app:

```
MANAGER_PASSCODE="whatever-you-want" python3 app.py
```

If you deploy it to a hosting service (below), set `MANAGER_PASSCODE` as
an environment variable in that service's settings screen instead.

You should also set a `SECRET_KEY` (any long random string) as an
environment variable when you deploy for real — it keeps your login
sessions secure:

```
SECRET_KEY="some-long-random-string-here" MANAGER_PASSCODE="whatever-you-want" python3 app.py
```

## Deploying it so it's reachable from anywhere (not just your computer)

Right now, running it on your own computer only works if the employee's
phone can reach that computer, which normally isn't the case. To make the
link actually work for employees away from your restaurant's wifi, you
need to put the app on a small hosting service. The easiest free option:

### Option: Render.com (free tier available)

1. Create a free account at render.com.
2. Create a new "Web Service."
3. Upload/connect this folder (Render can take a zip or a GitHub repo —
   if you don't use GitHub, ask me and I can help you set that up too).
4. Build command: `pip install -r requirements.txt`
5. Start command: `python3 app.py`
6. In the "Environment" settings, add:
   - `MANAGER_PASSCODE` = your chosen passcode
   - `SECRET_KEY` = any long random string
   - `PORT` = Render sets this automatically, you can leave it
7. Deploy. Render gives you a permanent web address (like
   `https://indigo-payroll.onrender.com`) — that's the address employees'
   confirmation links will use automatically once the app is live there.

Note: Render's free tier "sleeps" the app after inactivity and takes ~30
seconds to wake back up on the next visit — fine for something used a few
times a week, but worth knowing so a link doesn't seem broken if it's
just waking up.

If you'd rather use a different host (Railway, PythonAnywhere, a VPS,
etc.) the steps are similar — just tell me which one and I can walk you
through it.

## Where your data lives

Everything is stored in a single file, `payroll.db`, in this folder (a
SQLite database — nothing fancy, just a simple local file). On a hosting
service like Render, make sure you use their "persistent disk" option if
offered, otherwise the data resets each time the app restarts. If you
want, ask me later about swapping this for a database that's automatically
backed up.

## What's next (not built yet)

- The Catering Orders module (Module 1 of the spec) — still just a
  written spec, not built.
- Automatic text sending via a provider like Twilio, if you ever want it.
- Direct import of the CSV export into next month's Excel workbook
  (right now it's a manual copy-paste or import step).

Just let me know when you're ready for any of these.
