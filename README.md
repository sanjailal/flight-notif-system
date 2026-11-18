# Alliance Air Agatti -> Kochi alert (Dec 16-19, 2026)

Checks bookme.allianceair.in every 15 min (and Google Flights via SerpApi once a day)
and sends a push via ntfy.sh when a 9I flight appears or its fare changes.

## Setup
1. Install the **ntfy** app on your phone and subscribe to your private topic.
2. Repo secrets: `NTFY_TOPIC` (long random string), `SERPAPI_KEY`.
3. Run the workflow once manually (Actions -> flight-check -> Run workflow).

Local test: `python3 check_flights.py --dry-run`
Push test: `NTFY_TOPIC=<topic> python3 check_flights.py --test-notify`
Edit `DATES`/`ORIGIN`/`DEST` env vars to change the search.
