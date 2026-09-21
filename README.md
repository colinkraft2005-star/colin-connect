# Rush Tracker

A no-frills internal tool for rush: check a guy in with a photo, let brothers vote and comment, pull a ranked list at the end of the night.

## How it works

- **Check-In Station**: the brother running the door opens this page on a laptop/iPad, snaps a photo with the built-in camera input, and fills in name, year, major, phone, who brought him, and any notes.
- **Vote & Comment**: any brother with the house PIN can browse PNMs, thumbs up/down (tap again to undo or switch), and leave comments. Votes are tied to the name you enter at login, so no double-voting.
- **Leaderboard / Export**: sorts everyone by net score (up minus down) and lets you download a CSV for the rush chairs.

The whole site sits behind a single shared house PIN, entered once per browser session.

## Running it locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

The database is a single local file, `rush.db`, created automatically the first time you run it. Photos are stored inside that file too, so there's nothing else to configure.

## Setting the house PIN

Don't leave the PIN as `changeme`. Create a file at `.streamlit/secrets.toml` (locally) with:

```toml
HOUSE_PIN = "your_pin_here"
```

## Deploying (Streamlit Community Cloud)

Same flow you used for HoopsHub:

1. Push this folder to a GitHub repo.
2. Go to share.streamlit.io, connect the repo, set the main file to `app.py`.
3. In the app's Settings > Secrets, add `HOUSE_PIN = "your_pin_here"`.
4. Deploy. Share the URL and the PIN with the house.

One thing to know: Streamlit Community Cloud's filesystem isn't guaranteed to persist forever across redeploys, so `rush.db` (including photos) can reset if the app restarts or gets redeployed. Fine for a single rush period, but download the CSV export at the end of each night, and if you want it to survive long-term, swap the SQLite connection for something like Neon or Firebase (you're already set up with both for HoopsHub, so the same accounts work here).

## Reasonable next additions

- Multiple rush events/dates so you can filter "who came to Wednesday's event"
- A bid-list flag once someone's above a vote threshold
- Photo required (currently optional so the form doesn't block if the camera fails)
